# -*- coding: utf-8 -*-

import os
from subprocess import Popen, PIPE
import sys
import threading
import codecs
import locale
from functools import partial

try:
    import pty
except ImportError:
    # TODO: store exception in case it blows up in unexpected ways.
    # Typically we just expect it'll work fine on Unix and not at all on
    # Windows.
    pty = None

from .exceptions import Failure
from .platform import WINDOWS

from .vendor import six


def normalize_hide(val):
    hide_vals = (None, False, 'out', 'stdout', 'err', 'stderr', 'both', True)
    if val not in hide_vals:
        err = "'hide' got {0!r} which is not in {1!r}"
        raise ValueError(err.format(val, hide_vals))
    if val in (None, False):
        hide = ()
    elif val in ('both', True):
        hide = ('out', 'err')
    elif val == 'stdout':
        hide = ('out',)
    elif val == 'stderr':
        hide = ('err',)
    else:
        hide = (val,)
    return hide


# TODO: remove 'exception' field in run_* return values, if we don't run into
# situations similar to the one found in pexpect re: spurious IOErrors on Linux
# w/ PTYs. See #37 / 45db03ed8343ac97beefb360634f8106de92c6d7

class Runner(object):
    """
    Partially-abstract core command-running API.

    This class is not usable by itself and must be subclassed, implementing a
    number of methods such as `start`, `wait` and `get_returncode`. For a
    subclass implementation example, see the source code for `.Local`.
    """
    def __init__(self, context):
        """
        Create a new runner with a handle on some `.Context`.

        :param context:
            a `.Context` instance, used to transmit default options and provide
            access to other contextualized information (e.g. a remote-oriented
            `.Runner` might want a `.Context` subclass holding info about
            hostnames and ports.)

            .. note::
                The `.Context` given to `.Runner` instances **must** contain
                default config values for the `.Runner` class in question. At a
                minimum, this means values for each of the default
                `.Runner.run` keyword arguments such as ``echo`` and ``warn``.

        :raises exceptions.ValueError:
            if not all expected default values are found in ``context``.
        """
        #: The `.Context` given to the same-named argument of `__init__`.
        self.context = context
        # Bookkeeping re: whether pty fallback warning has been emitted.
        self.warned_about_pty_fallback = False

    def run(self, command, **kwargs):
        """
        Execute ``command``, returning a `Result` object.

        .. note::
            All kwargs will default to the values found in this instance's
            `~.Runner.context` attribute, specifically in its configuration's
            ``run`` subtree (e.g. ``run.echo`` provides the default value for
            the ``echo`` keyword, etc). The base default values are described
            in the parameter list below.

        :param str command: The shell command to execute.

        :param bool warn:
            Whether to warn and continue, instead of raising `.Failure`, when
            the executed command exits with a nonzero status. Default:
            ``False``.

        :param hide:
            Allows the caller to disable ``run``'s default behavior of copying
            the subprocess' stdout and stderr to the controlling terminal.
            Specify ``hide='out'`` (or ``'stdout'``) to hide only the stdout
            stream, ``hide='err'`` (or ``'stderr'``) to hide only stderr, or
            ``hide='both'`` (or ``True``) to hide both streams.

            The default value is ``None``, meaning to print everything;
            ``False`` will also disable hiding.

            .. note::
                Stdout and stderr are always captured and stored in the
                ``Result`` object, regardless of ``hide``'s value.

        :param bool pty:
            By default, ``run`` connects directly to the invoked process and
            reads its stdout/stderr streams. Some programs will buffer (or even
            behave) differently in this situation compared to using an actual
            terminal or pty. To use a pty, specify ``pty=True``.

            .. warning::
                Due to their nature, ptys have a single output stream, so the
                ability to tell stdout apart from stderr is **not possible**
                when ``pty=True``. As such, all output will appear on
                ``out_stream`` (see below) and be captured into the ``stdout``
                result attribute. ``err_stream`` and ``stderr`` will always be
                empty when ``pty=True``.

        :param bool fallback:
            Controls auto-fallback behavior re: problems offering a pty when
            ``pty=True``. Whether this has any effect depends on the specific
            `Runner` subclass being invoked. Default: ``True``.

        :param bool echo:
            Controls whether `.run` prints the command string to local stdout
            prior to executing it. Default: ``False``.

        :param str encoding:
            Override auto-detection of which encoding the subprocess is using
            for its stdout/stderr streams. Defaults to the return value of
            ``locale.getpreferredencoding(False)``).

        :param out_stream:
            A file-like stream object to which the subprocess' standard error
            should be written. If ``None`` (the default), ``sys.stdout`` will
            be used.

        :param err_stream:
            Same as ``out_stream``, except for standard error, and defaulting
            to ``sys.stderr``.

        :returns: `Result`

        :raises: `.Failure` (if the command exited nonzero & ``warn=False``)
        """
        exception = False
        # Normalize kwargs w/ config
        opts = {}
        for key, value in six.iteritems(self.context.config.run):
            runtime = kwargs.pop(key, None)
            opts[key] = value if runtime is None else runtime
        # TODO: handle invalid kwarg keys (anything left in kwargs)
        # Normalize 'hide' from one of the various valid input values
        opts['hide'] = normalize_hide(opts['hide'])
        # Derive stream objects
        out_stream = opts['out_stream']
        if out_stream is None:
            out_stream = sys.stdout
        err_stream = opts['err_stream']
        if err_stream is None:
            err_stream = sys.stderr
        # Echo
        if opts['echo']:
            print("\033[1;37m{0}\033[0m".format(command))
        # Determine pty or no
        self.using_pty = self.should_use_pty(opts['pty'], opts['fallback'])
        # Initiate command & kick off IO threads
        self.start(command)
        encoding = opts['encoding']
        if encoding is None:
            encoding = locale.getpreferredencoding(False)
        stdout, stderr = [], []
        threads = []
        argses = [
            (self.stdout_reader, out_stream, stdout, 'out' in opts['hide'],
                encoding),
        ]
        if not self.using_pty:
            argses.append(
                (self.stderr_reader, err_stream, stderr, 'err' in opts['hide'],
                    encoding),
            )
        for args in argses:
            t = threading.Thread(target=self._mux, args=args)
            threads.append(t)
            t.start()
        # Wait for completion, then tie things off & obtain result
        self.wait()
        for t in threads:
            t.join()
        stdout = ''.join(stdout)
        stderr = ''.join(stderr)
        if WINDOWS:
            # "Universal newlines" - replace all standard forms of
            # newline with \n. This is not technically Windows related
            # (\r as newline is an old Mac convention) but we only apply
            # the translation for Windows as that's the only platform
            # it is likely to matter for these days.
            stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
            stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
        # Get return/exit code
        exited = self.returncode()
        # Return, or raise as failure, our final result
        result = Result(
            stdout=stdout,
            stderr=stderr,
            exited=exited,
            pty=self.using_pty,
            exception=exception,
        )
        if not (result or opts['warn']):
            raise Failure(result)
        return result

    def should_use_pty(self, pty, fallback):
        """
        Should execution attempt to use a pseudo-terminal?

        :param bool pty:
            Whether the user explicitly asked for a pty.
        :param bool fallback:
            Whether falling back to non-pty execution should be allowed, in
            situations where ``pty=True`` but a pty could not be allocated.
        """
        # NOTE: fallback not used: no falling back implemented by default.
        return pty


class Local(Runner):
    """
    Execute a command on the local system in a subprocess.

    .. note::
        When Invoke itself is executed without a valid PTY (i.e.
        ``os.isatty(sys.stdin)`` is ``False``), it's not possible to present a
        handle on our PTY to local subprocesses. In such situations, `Local`
        will fallback to behaving as if ``pty=False``, on the theory that
        degraded execution is better than none at all, as well as printing a
        warning to stderr.

        To disable this behavior (i.e. if ``os.isatty`` is causing false
        negatives in your environment), say ``fallback=False``.
    """
    def should_use_pty(self, pty=False, fallback=True):
        use_pty = False
        if pty:
            use_pty = True
            if not os.isatty(sys.stdin.fileno()) and fallback:
                if not self.warned_about_pty_fallback:
                    sys.stderr.write("WARNING: stdin is not a pty; falling back to non-pty execution!\n") # noqa
                    self.warned_about_pty_fallback = True
                use_pty = False
        return use_pty

    # TODO: refactor into eg self._get_reader
    @property
    def stdout_reader(self):
        if self.using_pty:
            return partial(os.read, self.parent_fd)
        else:
            return partial(os.read, self.process.stdout.fileno())

    # TODO: ditto
    @property
    def stderr_reader(self):
        return partial(os.read, self.process.stderr.fileno())

    def _mux(self, read_func, dest, buffer_, hide, encoding):
        # Inner generator yielding read data
        def get():
            while True:
                # self.read_stream
                data = read_func(1000)
                if not data:
                    break
                # Sometimes os.read gives us bytes under Python 3...and
                # sometimes it doesn't. ¯\_(ツ)_/¯
                if not isinstance(data, six.binary_type):
                    # Can't use six.b because that just assumes latin-1 :(
                    data = data.encode(encoding)
                yield data
        # Decode stream using our generator & requested encoding
        for data in codecs.iterdecode(get(), encoding, errors='replace'):
            if not hide:
                dest.write(data)
                dest.flush()
            buffer_.append(data)

    def start(self, command):
        if self.using_pty:
            # TODO: re-insert Windows "lol y u no pty" stuff at this point,
            # if 'pty' is None.
            self.pid, self.parent_fd = pty.fork()
            # If we're the child process, load up the actual command in a
            # shell, just as subprocess does; this replaces our process - whose
            # pipes are all hooked up to the PTY - with the "real" one.
            if self.pid == 0:
                # Use execv for bare-minimum "exec w/ variable # args"
                # behavior. No need for the 'p' (use PATH to find executable)
                # or 'e' (define a custom/overridden shell env) variants, for
                # now.
                # TODO: use /bin/sh or whatever subprocess does. Only using
                # bash for now because that's what we have been testing
                # against.
                # TODO: also see if subprocess is using equivalent of execvp...
                # TODO: both pty.spawn() and pexpect.spawn() do a lot of
                # setup/teardown involving tty.*, setwinsize, getrlimit,
                # signal. Ostensibly we'll want some of that eventually, but if
                # possible write tests - integration-level if necessary -
                # before adding it!
                os.execv('/bin/bash', ['/bin/bash', '-c', command])
        else:
            self.process = Popen(
                command,
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
            )

    def wait(self):
        if self.using_pty:
            while True:
                # TODO: set 2nd value to os.WNOHANG in some situations?
                pid_val, self.status = os.waitpid(self.pid, 0)
                # waitpid() sets the 'pid' return val to 0 when no children have
                # exited yet; when it is NOT zero, we know the child's stopped.
                if pid_val != 0:
                    break
                # TODO: io sleep?
        else:
            self.process.wait()

    def returncode(self):
        if self.using_pty:
            return os.WEXITSTATUS(self.status)
        else:
            return self.process.returncode


class Result(object):
    """
    A container for information about the result of a command execution.

    `Result` instances have the following attributes:

    * ``stdout``: The subprocess' standard output, as a multiline string.
    * ``stderr``: Same as ``stdout`` but containing standard error (unless
      the process was invoked via a pty; see `.Runner.run`.)
    * ``exited``: An integer representing the subprocess' exit/return code.
    * ``return_code``: An alias to ``exited``.
    * ``ok``: A boolean equivalent to ``exited == 0``.
    * ``failed``: The inverse of ``ok``: ``True`` if the program exited with a
      nonzero return code.
    * ``pty``: A boolean describing whether the subprocess was invoked with a
      pty or not; see `.Runner.run`.
    * ``exception``: Typically ``None``, but may be an exception object if
      ``pty`` was ``True`` and ``run`` had to swallow an apparently-spurious
      ``OSError``. Solely for sanity checking/debugging purposes.

    `Result` objects' truth evaluation is equivalent to their ``ok``
    attribute's value.
    """
    # TODO: inherit from namedtuple instead? heh
    def __init__(self, stdout, stderr, exited, pty, exception=None):
        self.exited = self.return_code = exited
        self.stdout = stdout
        self.stderr = stderr
        self.pty = pty
        self.exception = exception

    def __nonzero__(self):
        # Holy mismatch between name and implementation, Batman!
        return self.exited == 0

    # Python 3 ahoy
    def __bool__(self):
        return self.__nonzero__()

    def __str__(self):
        ret = ["Command exited with status {0}.".format(self.exited)]
        for x in ('stdout', 'stderr'):
            val = getattr(self, x)
            ret.append("""=== {0} ===
{1}
""".format(x, val.rstrip()) if val else "(no {0})".format(x))
        return "\n".join(ret)

    @property
    def ok(self):
        return self.exited == 0

    @property
    def failed(self):
        return not self.ok
