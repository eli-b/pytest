""" (disabled by default) support for testing pytest and pytest plugins. """
import sys
import os
import codecs
import re
import time
import platform
from fnmatch import fnmatch
import subprocess

import py
import pytest
from py.builtin import print_
from _pytest.core import HookCaller, add_method_wrapper

from _pytest.main import Session, EXIT_OK


def get_public_names(l):
    """Only return names from iterator l without a leading underscore."""
    return [x for x in l if x[0] != "_"]


def pytest_addoption(parser):
    group = parser.getgroup("pylib")
    group.addoption('--no-tools-on-path',
           action="store_true", dest="notoolsonpath", default=False,
           help=("discover tools on PATH instead of going through py.cmdline.")
    )

def pytest_configure(config):
    # This might be called multiple times. Only take the first.
    global _pytest_fullpath
    try:
        _pytest_fullpath
    except NameError:
        _pytest_fullpath = os.path.abspath(pytest.__file__.rstrip("oc"))
        _pytest_fullpath = _pytest_fullpath.replace("$py.class", ".py")


class ParsedCall:
    def __init__(self, name, kwargs):
        self.__dict__.update(kwargs)
        self._name = name

    def __repr__(self):
        d = self.__dict__.copy()
        del d['_name']
        return "<ParsedCall %r(**%r)>" %(self._name, d)


class HookRecorder:
    """Record all hooks called in a plugin manager.

    This wraps all the hook calls in the plugin manager, recording
    each call before propagating the normal calls.

    """

    def __init__(self, pluginmanager):
        self._pluginmanager = pluginmanager
        self.calls = []

        def _docall(hookcaller, methods, kwargs):
            self.calls.append(ParsedCall(hookcaller.name, kwargs))
            yield
        self._undo_wrapping = add_method_wrapper(HookCaller, _docall)
        pluginmanager.add_shutdown(self._undo_wrapping)

    def finish_recording(self):
        self._undo_wrapping()

    def getcalls(self, names):
        if isinstance(names, str):
            names = names.split()
        return [call for call in self.calls if call._name in names]

    def assert_contains(self, entries):
        __tracebackhide__ = True
        i = 0
        entries = list(entries)
        backlocals = sys._getframe(1).f_locals
        while entries:
            name, check = entries.pop(0)
            for ind, call in enumerate(self.calls[i:]):
                if call._name == name:
                    print_("NAMEMATCH", name, call)
                    if eval(check, backlocals, call.__dict__):
                        print_("CHECKERMATCH", repr(check), "->", call)
                    else:
                        print_("NOCHECKERMATCH", repr(check), "-", call)
                        continue
                    i += ind + 1
                    break
                print_("NONAMEMATCH", name, "with", call)
            else:
                pytest.fail("could not find %r check %r" % (name, check))

    def popcall(self, name):
        __tracebackhide__ = True
        for i, call in enumerate(self.calls):
            if call._name == name:
                del self.calls[i]
                return call
        lines = ["could not find call %r, in:" % (name,)]
        lines.extend(["  %s" % str(x) for x in self.calls])
        pytest.fail("\n".join(lines))

    def getcall(self, name):
        l = self.getcalls(name)
        assert len(l) == 1, (name, l)
        return l[0]

    # functionality for test reports

    def getreports(self,
                   names="pytest_runtest_logreport pytest_collectreport"):
        return [x.report for x in self.getcalls(names)]

    def matchreport(self, inamepart="",
        names="pytest_runtest_logreport pytest_collectreport", when=None):
        """ return a testreport whose dotted import path matches """
        l = []
        for rep in self.getreports(names=names):
            try:
                if not when and rep.when != "call" and rep.passed:
                    # setup/teardown passing reports - let's ignore those
                    continue
            except AttributeError:
                pass
            if when and getattr(rep, 'when', None) != when:
                continue
            if not inamepart or inamepart in rep.nodeid.split("::"):
                l.append(rep)
        if not l:
            raise ValueError("could not find test report matching %r: "
                             "no test reports at all!" % (inamepart,))
        if len(l) > 1:
            raise ValueError(
                "found 2 or more testreports matching %r: %s" %(inamepart, l))
        return l[0]

    def getfailures(self,
                    names='pytest_runtest_logreport pytest_collectreport'):
        return [rep for rep in self.getreports(names) if rep.failed]

    def getfailedcollections(self):
        return self.getfailures('pytest_collectreport')

    def listoutcomes(self):
        passed = []
        skipped = []
        failed = []
        for rep in self.getreports(
            "pytest_collectreport pytest_runtest_logreport"):
            if rep.passed:
                if getattr(rep, "when", None) == "call":
                    passed.append(rep)
            elif rep.skipped:
                skipped.append(rep)
            elif rep.failed:
                failed.append(rep)
        return passed, skipped, failed

    def countoutcomes(self):
        return [len(x) for x in self.listoutcomes()]

    def assertoutcome(self, passed=0, skipped=0, failed=0):
        realpassed, realskipped, realfailed = self.listoutcomes()
        assert passed == len(realpassed)
        assert skipped == len(realskipped)
        assert failed == len(realfailed)

    def clear(self):
        self.calls[:] = []


def pytest_funcarg__linecomp(request):
    return LineComp()

def pytest_funcarg__LineMatcher(request):
    return LineMatcher

def pytest_funcarg__testdir(request):
    tmptestdir = TmpTestdir(request)
    return tmptestdir

rex_outcome = re.compile("(\d+) (\w+)")
class RunResult:
    """The result of running a command.

    Attributes:

    :ret: The return value.
    :outlines: List of lines captured from stdout.
    :errlines: List of lines captures from stderr.
    :stdout: LineMatcher of stdout, use ``stdout.str()`` to
       reconstruct stdout or the commonly used
       ``stdout.fnmatch_lines()`` method.
    :stderrr: LineMatcher of stderr.
    :duration: Duration in seconds.

    """
    def __init__(self, ret, outlines, errlines, duration):
        self.ret = ret
        self.outlines = outlines
        self.errlines = errlines
        self.stdout = LineMatcher(outlines)
        self.stderr = LineMatcher(errlines)
        self.duration = duration

    def parseoutcomes(self):
        for line in reversed(self.outlines):
            if 'seconds' in line:
                outcomes = rex_outcome.findall(line)
                if outcomes:
                    d = {}
                    for num, cat in outcomes:
                        d[cat] = int(num)
                    return d

class TmpTestdir:
    """Temporary test directory with tools to test/run py.test itself.

    This is based on the ``tmpdir`` fixture but provides a number of
    methods which aid with testing py.test itself.  Unless
    :py:meth:`chdir` is used all methods will use :py:attr:`tmpdir` as
    current working directory.

    Attributes:

    :tmpdir: The :py:class:`py.path.local` instance of the temporary
       directory.

    :plugins: A list of plugins to use with :py:meth:`parseconfig` and
       :py:meth:`runpytest`.  Initially this is an empty list but
       plugins can be added to the list.  The type of items to add to
       the list depend on the method which uses them so refer to them
       for details.

    """

    def __init__(self, request):
        self.request = request
        self.Config = request.config.__class__
        # XXX remove duplication with tmpdir plugin
        basetmp = request.config._tmpdirhandler.ensuretemp("testdir")
        name = request.function.__name__
        for i in range(100):
            try:
                tmpdir = basetmp.mkdir(name + str(i))
            except py.error.EEXIST:
                continue
            break
        self.tmpdir = tmpdir
        self.plugins = []
        self._savesyspath = list(sys.path)
        self.chdir() # always chdir
        self.request.addfinalizer(self.finalize)

    def __repr__(self):
        return "<TmpTestdir %r>" % (self.tmpdir,)

    def finalize(self):
        """Clean up global state artifacts.

        Some methods modify the global interpreter state and this
        tries to clean this up.  It does not remove the temporary
        directlry however so it can be looked at after the test run
        has finished.

        """
        sys.path[:] = self._savesyspath
        if hasattr(self, '_olddir'):
            self._olddir.chdir()
        # delete modules that have been loaded from tmpdir
        for name, mod in list(sys.modules.items()):
            if mod:
                fn = getattr(mod, '__file__', None)
                if fn and fn.startswith(str(self.tmpdir)):
                    del sys.modules[name]

    def make_hook_recorder(self, pluginmanager):
        """Create a new :py:class:`HookRecorder` for a PluginManager."""
        assert not hasattr(pluginmanager, "reprec")
        pluginmanager.reprec = reprec = HookRecorder(pluginmanager)
        self.request.addfinalizer(reprec.finish_recording)
        return reprec

    def chdir(self):
        """Cd into the temporary directory.

        This is done automatically upon instantiation.

        """
        old = self.tmpdir.chdir()
        if not hasattr(self, '_olddir'):
            self._olddir = old

    def _makefile(self, ext, args, kwargs):
        items = list(kwargs.items())
        if args:
            source = py.builtin._totext("\n").join(
                map(py.builtin._totext, args)) + py.builtin._totext("\n")
            basename = self.request.function.__name__
            items.insert(0, (basename, source))
        ret = None
        for name, value in items:
            p = self.tmpdir.join(name).new(ext=ext)
            source = py.code.Source(value)
            def my_totext(s, encoding="utf-8"):
                if py.builtin._isbytes(s):
                    s = py.builtin._totext(s, encoding=encoding)
                return s
            source_unicode = "\n".join([my_totext(line) for line in source.lines])
            source = py.builtin._totext(source_unicode)
            content = source.strip().encode("utf-8") # + "\n"
            #content = content.rstrip() + "\n"
            p.write(content, "wb")
            if ret is None:
                ret = p
        return ret

    def makefile(self, ext, *args, **kwargs):
        """Create a new file in the testdir.

        ext: The extension the file should use, including the dot.
           E.g. ".py".

        args: All args will be treated as strings and joined using
           newlines.  The result will be written as contents to the
           file.  The name of the file will be based on the test
           function requesting this fixture.
           E.g. "testdir.makefile('.txt', 'line1', 'line2')"

        kwargs: Each keyword is the name of a file, while the value of
           it will be written as contents of the file.
           E.g. "testdir.makefile('.ini', pytest='[pytest]\naddopts=-rs\n')"

        """
        return self._makefile(ext, args, kwargs)

    def makeconftest(self, source):
        """Write a contest.py file with 'source' as contents."""
        return self.makepyfile(conftest=source)

    def makeini(self, source):
        """Write a tox.ini file with 'source' as contents."""
        return self.makefile('.ini', tox=source)

    def getinicfg(self, source):
        """Return the pytest section from the tox.ini config file."""
        p = self.makeini(source)
        return py.iniconfig.IniConfig(p)['pytest']

    def makepyfile(self, *args, **kwargs):
        """Shortcut for .makefile() with a .py extension."""
        return self._makefile('.py', args, kwargs)

    def maketxtfile(self, *args, **kwargs):
        """Shortcut for .makefile() with a .txt extension."""
        return self._makefile('.txt', args, kwargs)

    def syspathinsert(self, path=None):
        """Prepend a directory to sys.path, defaults to :py:attr:`tmpdir`.

        This is undone automatically after the test.
        """
        if path is None:
            path = self.tmpdir
        sys.path.insert(0, str(path))

    def mkdir(self, name):
        """Create a new (sub)directory."""
        return self.tmpdir.mkdir(name)

    def mkpydir(self, name):
        """Create a new python package.

        This creates a (sub)direcotry with an empty ``__init__.py``
        file so that is recognised as a python package.

        """
        p = self.mkdir(name)
        p.ensure("__init__.py")
        return p

    Session = Session
    def getnode(self, config, arg):
        """Return the collection node of a file.

        :param config: :py:class:`_pytest.config.Config` instance, see
           :py:meth:`parseconfig` and :py:meth:`parseconfigure` to
           create the configuration.

        :param arg: A :py:class:`py.path.local` instance of the file.

        """
        session = Session(config)
        assert '::' not in str(arg)
        p = py.path.local(arg)
        config.hook.pytest_sessionstart(session=session)
        res = session.perform_collect([str(p)], genitems=False)[0]
        config.hook.pytest_sessionfinish(session=session, exitstatus=EXIT_OK)
        return res

    def getpathnode(self, path):
        """Return the collection node of a file.

        This is like :py:meth:`getnode` but uses
        :py:meth:`parseconfigure` to create the (configured) py.test
        Config instance.

        :param path: A :py:class:`py.path.local` instance of the file.

        """
        config = self.parseconfigure(path)
        session = Session(config)
        x = session.fspath.bestrelpath(path)
        config.hook.pytest_sessionstart(session=session)
        res = session.perform_collect([x], genitems=False)[0]
        config.hook.pytest_sessionfinish(session=session, exitstatus=EXIT_OK)
        return res

    def genitems(self, colitems):
        """Generate all test items from a collection node.

        This recurses into the collection node and returns a list of
        all the test items contained within.

        """
        session = colitems[0].session
        result = []
        for colitem in colitems:
            result.extend(session.genitems(colitem))
        return result

    def runitem(self, source):
        """Run the "test_func" Item.

        The calling test instance (the class which contains the test
        method) must provide a ``.getrunner()`` method which should
        return a runner which can run the test protocol for a single
        item, like e.g. :py:func:`_pytest.runner.runtestprotocol`.

        """
        # used from runner functional tests
        item = self.getitem(source)
        # the test class where we are called from wants to provide the runner
        testclassinstance = self.request.instance
        runner = testclassinstance.getrunner()
        return runner(item)

    def inline_runsource(self, source, *cmdlineargs):
        """Run a test module in process using ``pytest.main()``.

        This run writes "source" into a temporary file and runs
        ``pytest.main()`` on it, returning a :py:class:`HookRecorder`
        instance for the result.

        :param source: The source code of the test module.

        :param cmdlineargs: Any extra command line arguments to use.

        :return: :py:class:`HookRecorder` instance of the result.

        """
        p = self.makepyfile(source)
        l = list(cmdlineargs) + [p]
        return self.inline_run(*l)

    def inline_runsource1(self, *args):
        """Run a test module in process using ``pytest.main()``.

        This behaves exactly like :py:meth:`inline_runsource` and
        takes identical arguments.  However the return value is a list
        of the reports created by the pytest_runtest_logreport hook
        during the run.

        """
        args = list(args)
        source = args.pop()
        p = self.makepyfile(source)
        l = list(args) + [p]
        reprec = self.inline_run(*l)
        reports = reprec.getreports("pytest_runtest_logreport")
        assert len(reports) == 3, reports # setup/call/teardown
        return reports[1]

    def inline_genitems(self, *args):
        """Run ``pytest.main(['--collectonly'])`` in-process.

        Retuns a tuple of the collected items and a
        :py:class:`HookRecorder` instance.

        """
        return self.inprocess_run(list(args) + ['--collectonly'])

    def inprocess_run(self, args, plugins=()):
        """Run ``pytest.main()`` in-process, return Items and a HookRecorder.

        This runs the :py:func:`pytest.main` function to run all of
        py.test inside the test process itself like
        :py:meth:`inline_run`.  However the return value is a tuple of
        the collection items and a :py:class:`HookRecorder` instance.

        """
        rec = self.inline_run(*args, plugins=plugins)
        items = [x.item for x in rec.getcalls("pytest_itemcollected")]
        return items, rec

    def inline_run(self, *args, **kwargs):
        """Run ``pytest.main()`` in-process, returning a HookRecorder.

        This runs the :py:func:`pytest.main` function to run all of
        py.test inside the test process itself.  This means it can
        return a :py:class:`HookRecorder` instance which gives more
        detailed results from then run then can be done by matching
        stdout/stderr from :py:meth:`runpytest`.

        :param args: Any command line arguments to pass to
           :py:func:`pytest.main`.

        :param plugin: (keyword-only) Extra plugin instances the
           ``pytest.main()`` instance should use.

        :return: A :py:class:`HookRecorder` instance.

        """
        rec = []
        class Collect:
            def pytest_configure(x, config):
                rec.append(self.make_hook_recorder(config.pluginmanager))
        plugins = kwargs.get("plugins") or []
        plugins.append(Collect())
        ret = pytest.main(list(args), plugins=plugins)
        assert len(rec) == 1
        reprec = rec[0]
        reprec.ret = ret
        return reprec

    def parseconfig(self, *args):
        """Return a new py.test Config instance from given commandline args.

        This invokes the py.test bootstrapping code in _pytest.config
        to create a new :py:class:`_pytest.core.PluginManager` and
        call the pytest_cmdline_parse hook to create new
        :py:class:`_pytest.config.Config` instance.

        If :py:attr:`plugins` has been populated they should be plugin
        modules which will be registered with the PluginManager.

        """
        args = [str(x) for x in args]
        for x in args:
            if str(x).startswith('--basetemp'):
                break
        else:
            args.append("--basetemp=%s" % self.tmpdir.dirpath('basetemp'))
        import _pytest.config
        config = _pytest.config._prepareconfig(args, self.plugins)
        # we don't know what the test will do with this half-setup config
        # object and thus we make sure it gets unconfigured properly in any
        # case (otherwise capturing could still be active, for example)
        def ensure_unconfigure():
            if hasattr(config.pluginmanager, "_config"):
                config.pluginmanager.do_unconfigure(config)
            config.pluginmanager.ensure_shutdown()

        self.request.addfinalizer(ensure_unconfigure)
        return config

    def parseconfigure(self, *args):
        """Return a new py.test configured Config instance.

        This returns a new :py:class:`_pytest.config.Config` instance
        like :py:meth:`parseconfig`, but also calls the
        pytest_configure hook.

        """
        config = self.parseconfig(*args)
        config.do_configure()
        self.request.addfinalizer(config.do_unconfigure)
        return config

    def getitem(self,  source, funcname="test_func"):
        """Return the test item for a test function.

        This writes the source to a python file and runs py.test's
        collection on the resulting module, returning the test item
        for the requested function name.

        :param source: The module source.

        :param funcname: The name of the test function for which the
           Item must be returned.

        """
        items = self.getitems(source)
        for item in items:
            if item.name == funcname:
                return item
        assert 0, "%r item not found in module:\n%s\nitems: %s" %(
                  funcname, source, items)

    def getitems(self,  source):
        """Return all test items collected from the module.

        This writes the source to a python file and runs py.test's
        collection on the resulting module, returning all test items
        contained within.

        """
        modcol = self.getmodulecol(source)
        return self.genitems([modcol])

    def getmodulecol(self,  source, configargs=(), withinit=False):
        """Return the module collection node for ``source``.

        This writes ``source`` to a file using :py:meth:`makepyfile`
        and then runs the py.test collection on it, returning the
        collection node for the test module.

        :param source: The source code of the module to collect.

        :param configargs: Any extra arguments to pass to
           :py:meth:`parseconfigure`.

        :param withinit: Whether to also write a ``__init__.py`` file
           to the temporarly directory to ensure it is a package.

        """
        kw = {self.request.function.__name__: py.code.Source(source).strip()}
        path = self.makepyfile(**kw)
        if withinit:
            self.makepyfile(__init__ = "#")
        self.config = config = self.parseconfigure(path, *configargs)
        node = self.getnode(config, path)
        return node

    def collect_by_name(self, modcol, name):
        """Return the collection node for name from the module collection.

        This will search a module collection node for a collection
        node matching the given name.

        :param modcol: A module collection node, see
           :py:meth:`getmodulecol`.

        :param name: The name of the node to return.

        """
        for colitem in modcol._memocollect():
            if colitem.name == name:
                return colitem

    def popen(self, cmdargs, stdout, stderr, **kw):
        """Invoke subprocess.Popen.

        This calls subprocess.Popen making sure the current working
        directory is the PYTHONPATH.

        You probably want to use :py:meth:`run` instead.

        """
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [
            str(os.getcwd()), env.get('PYTHONPATH', '')]))
        kw['env'] = env
        return subprocess.Popen(cmdargs,
                                stdout=stdout, stderr=stderr, **kw)

    def run(self, *cmdargs):
        """Run a command with arguments.

        Run a process using subprocess.Popen saving the stdout and
        stderr.

        Returns a :py:class:`RunResult`.

        """
        return self._run(*cmdargs)

    def _run(self, *cmdargs):
        cmdargs = [str(x) for x in cmdargs]
        p1 = self.tmpdir.join("stdout")
        p2 = self.tmpdir.join("stderr")
        print_("running", cmdargs, "curdir=", py.path.local())
        f1 = codecs.open(str(p1), "w", encoding="utf8")
        f2 = codecs.open(str(p2), "w", encoding="utf8")
        try:
            now = time.time()
            popen = self.popen(cmdargs, stdout=f1, stderr=f2,
                close_fds=(sys.platform != "win32"))
            ret = popen.wait()
        finally:
            f1.close()
            f2.close()
        f1 = codecs.open(str(p1), "r", encoding="utf8")
        f2 = codecs.open(str(p2), "r", encoding="utf8")
        try:
            out = f1.read().splitlines()
            err = f2.read().splitlines()
        finally:
            f1.close()
            f2.close()
        self._dump_lines(out, sys.stdout)
        self._dump_lines(err, sys.stderr)
        return RunResult(ret, out, err, time.time()-now)

    def _dump_lines(self, lines, fp):
        try:
            for line in lines:
                py.builtin.print_(line, file=fp)
        except UnicodeEncodeError:
            print("couldn't print to %s because of encoding" % (fp,))

    def runpybin(self, scriptname, *args):
        """Run a py.* tool with arguments.

        This can realy only be used to run py.test, you probably want
            :py:meth:`runpytest` instead.

        Returns a :py:class:`RunResult`.

        """
        fullargs = self._getpybinargs(scriptname) + args
        return self.run(*fullargs)

    def _getpybinargs(self, scriptname):
        if not self.request.config.getvalue("notoolsonpath"):
            # XXX we rely on script referring to the correct environment
            # we cannot use "(sys.executable,script)"
            # because on windows the script is e.g. a py.test.exe
            return (sys.executable, _pytest_fullpath,) # noqa
        else:
            pytest.skip("cannot run %r with --no-tools-on-path" % scriptname)

    def runpython(self, script, prepend=True):
        """Run a python script.

        If ``prepend`` is True then the directory from which the py
        package has been imported will be prepended to sys.path.

        Returns a :py:class:`RunResult`.

        """
        # XXX The prepend feature is probably not very useful since the
        #     split of py and pytest.
        if prepend:
            s = self._getsysprepend()
            if s:
                script.write(s + "\n" + script.read())
        return self.run(sys.executable, script)

    def _getsysprepend(self):
        if self.request.config.getvalue("notoolsonpath"):
            s = "import sys;sys.path.insert(0,%r);" % str(py._pydir.dirpath())
        else:
            s = ""
        return s

    def runpython_c(self, command):
        """Run python -c "command", return a :py:class:`RunResult`."""
        command = self._getsysprepend() + command
        return self.run(sys.executable, "-c", command)

    def runpytest(self, *args):
        """Run py.test as a subprocess with given arguments.

        Any plugins added to the :py:attr:`plugins` list will added
        using the ``-p`` command line option.  Addtionally
        ``--basetemp`` is used put any temporary files and directories
        in a numbered directory prefixed with "runpytest-" so they do
        not conflict with the normal numberd pytest location for
        temporary files and directories.

        Returns a :py:class:`RunResult`.

        """
        p = py.path.local.make_numbered_dir(prefix="runpytest-",
            keep=None, rootdir=self.tmpdir)
        args = ('--basetemp=%s' % p, ) + args
        #for x in args:
        #    if '--confcutdir' in str(x):
        #        break
        #else:
        #    pass
        #    args = ('--confcutdir=.',) + args
        plugins = [x for x in self.plugins if isinstance(x, str)]
        if plugins:
            args = ('-p', plugins[0]) + args
        return self.runpybin("py.test", *args)

    def spawn_pytest(self, string, expect_timeout=10.0):
        """Run py.test using pexpect.

        This makes sure to use the right py.test and sets up the
        temporary directory locations.

        The pexpect child is returned.

        """
        if self.request.config.getvalue("notoolsonpath"):
            pytest.skip("--no-tools-on-path prevents running pexpect-spawn tests")
        basetemp = self.tmpdir.mkdir("pexpect")
        invoke = " ".join(map(str, self._getpybinargs("py.test")))
        cmd = "%s --basetemp=%s %s" % (invoke, basetemp, string)
        return self.spawn(cmd, expect_timeout=expect_timeout)

    def spawn(self, cmd, expect_timeout=10.0):
        """Run a command using pexpect.

        The pexpect child is returned.
        """
        pexpect = pytest.importorskip("pexpect", "3.0")
        if hasattr(sys, 'pypy_version_info') and '64' in platform.machine():
            pytest.skip("pypy-64 bit not supported")
        if sys.platform == "darwin":
            pytest.xfail("pexpect does not work reliably on darwin?!")
        if sys.platform.startswith("freebsd"):
            pytest.xfail("pexpect does not work reliably on freebsd")
        logfile = self.tmpdir.join("spawn.out").open("wb")
        child = pexpect.spawn(cmd, logfile=logfile)
        self.request.addfinalizer(logfile.close)
        child.timeout = expect_timeout
        return child

def getdecoded(out):
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            return "INTERNAL not-utf8-decodeable, truncated string:\n%s" % (
                    py.io.saferepr(out),)


class LineComp:
    def __init__(self):
        self.stringio = py.io.TextIO()

    def assert_contains_lines(self, lines2):
        """ assert that lines2 are contained (linearly) in lines1.
            return a list of extralines found.
        """
        __tracebackhide__ = True
        val = self.stringio.getvalue()
        self.stringio.truncate(0)
        self.stringio.seek(0)
        lines1 = val.split("\n")
        return LineMatcher(lines1).fnmatch_lines(lines2)

class LineMatcher:
    """Flexible matching of text.

    This is a convenience class to test large texts like the output of
    commands.

    The constructor takes a list of lines without their trailing
    newlines, i.e. ``text.splitlines()``.

    """

    def __init__(self,  lines):
        self.lines = lines

    def str(self):
        """Return the entire original text."""
        return "\n".join(self.lines)

    def _getlines(self, lines2):
        if isinstance(lines2, str):
            lines2 = py.code.Source(lines2)
        if isinstance(lines2, py.code.Source):
            lines2 = lines2.strip().lines
        return lines2

    def fnmatch_lines_random(self, lines2):
        """Check lines exist in the output.

        The argument is a list of lines which have to occur in the
        output, in any order.  Each line can contain glob whildcards.

        """
        lines2 = self._getlines(lines2)
        for line in lines2:
            for x in self.lines:
                if line == x or fnmatch(x, line):
                    print_("matched: ", repr(line))
                    break
            else:
                raise ValueError("line %r not found in output" % line)

    def get_lines_after(self, fnline):
        """Return all lines following the given line in the text.

        The given line can contain glob wildcards.
        """
        for i, line in enumerate(self.lines):
            if fnline == line or fnmatch(line, fnline):
                return self.lines[i+1:]
        raise ValueError("line %r not found in output" % fnline)

    def fnmatch_lines(self, lines2):
        """Search the text for matching lines.

        The argument is a list of lines which have to match and can
        use glob wildcards.  If they do not match an pytest.fail() is
        called.  The matches and non-matches are also printed on
        stdout.

        """
        def show(arg1, arg2):
            py.builtin.print_(arg1, arg2, file=sys.stderr)
        lines2 = self._getlines(lines2)
        lines1 = self.lines[:]
        nextline = None
        extralines = []
        __tracebackhide__ = True
        for line in lines2:
            nomatchprinted = False
            while lines1:
                nextline = lines1.pop(0)
                if line == nextline:
                    show("exact match:", repr(line))
                    break
                elif fnmatch(nextline, line):
                    show("fnmatch:", repr(line))
                    show("   with:", repr(nextline))
                    break
                else:
                    if not nomatchprinted:
                        show("nomatch:", repr(line))
                        nomatchprinted = True
                    show("    and:", repr(nextline))
                extralines.append(nextline)
            else:
                pytest.fail("remains unmatched: %r, see stderr" % (line,))
