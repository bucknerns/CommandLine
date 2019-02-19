from io import BytesIO
import atexit
import os
import shlex
import signal
import subprocess
import threading
import time

import psutil

import platform

is_windows = platform.system().lower() == "windows"
OBJS_TO_CLEANUP = []


def cleanup():
    for obj in list(OBJS_TO_CLEANUP):
        obj.kill()


atexit.register(cleanup)


def read_pipe(pipe, fp_out):
    def target():
        for line in iter(pipe.readline, b""):
            fp_out.write(line)
    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    return thread


class CLIResponse:
    def __init__(self, cmd, returncode=None, proc=None):
        self.stderr_buf = BytesIO()
        self.stdout_buf = BytesIO()
        self.cmd = cmd
        self.proc = proc
        self.returncode = returncode
        self.decode_type = "utf8"
        self.total_time = None

    @property
    def stdout(self):
        return self.stdout_buf.getvalue()

    @property
    def stderr(self):
        return self.stderr_buf.getvalue()

    @property
    def stdout_str(self):
        return self.stdout.decode(self.decode_type, errors="ignore").rstrip()

    @property
    def stderr_str(self):
        return self.stderr.decode(self.decode_type, errors="ignore").rstrip()

    @property
    def is_running(self):
        if self.proc:
            try:
                os.getpgid(self.proc.pid)
                return True
            except ProcessLookupError:
                return False
        return False

    def __str__(self):
        try:
            stdout = self.stdout.decode(self.decode_type)
        except UnicodeDecodeError:
            stdout = "<Binary data>"

        try:
            stderr = self.stderr.decode(self.decode_type)
        except UnicodeDecodeError:
            stderr = "<Binary data>"
        sep = 40 * "="
        return (
            f"\n{sep}\nCommand: {self.cmd}\n{sep}\nRunning: {self.is_running}"
            f"\nReturn code: {self.returncode}\nStdout:\n{stdout}\n"
            f"Stderr:\n{stderr}\n{sep}\n")

    def __repr__(self):
        return self.__str__()

    def kill(self, sig=None):
        if self.proc is None:
            return
        if sig is None:
            if is_windows:
                sig = signal.SIGTERM
            else:
                sig = signal.SIGKILL
        try:
            for proc in psutil.Process(self.proc.pid).children(recursive=True):
                os.kill(proc.pid, sig)
            os.kill(self.proc.pid, sig)
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass
        if self in OBJS_TO_CLEANUP:
            OBJS_TO_CLEANUP.remove(self)

    def __del__(self):
        self.kill()

    def clear_buffers(self):
        self.stdout_buf.seek(0)
        self.stdout_buf.truncate()
        self.stderr_buf.seek(0)
        self.stderr_buf.truncate()


class CommandLineClient:
    @classmethod
    def _exec_cmd(cls, cmd, timeout=None, cmd_obj=None, shell=False):
        cmd_obj = cmd_obj or CLIResponse(cmd=cmd)
        if shell is False:
            cmd = shlex.split(cmd)
        start = time.time()

        def target():
            pipe = subprocess.PIPE
            kwargs = {
                "stdout": pipe, "stderr": pipe,
                "stdin": open(os.devnull, "rb"), "shell": shell}
            if not is_windows:
                kwargs["preexec_fn"] = os.setsid
            cmd_obj.proc = subprocess.Popen(cmd, **kwargs)
            tout = read_pipe(cmd_obj.proc.stdout, cmd_obj.stdout_buf)
            terr = read_pipe(cmd_obj.proc.stderr, cmd_obj.stderr_buf)
            cmd_obj.returncode = cmd_obj.proc.wait()
            tout.join()
            terr.join()

        if timeout is not None:
            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            thread.join(timeout)
            if thread.is_alive():
                cmd_obj.kill()
                thread.join()
        else:
            target()
        cmd_obj.returncode = cmd_obj.proc.returncode
        cmd_obj.total_time = time.time() - start
        return cmd_obj

    @classmethod
    def exec_cmd(cls, cmd, timeout=None, shell=False):
        obj = cls._exec_cmd(cmd=cmd, timeout=timeout, shell=shell)
        return obj

    @classmethod
    def async_cmd(cls, cmd, timeout=None, shell=False):
        obj = CLIResponse(cmd)
        thread = threading.Thread(target=cls._exec_cmd, kwargs={
            "cmd": cmd, "timeout": timeout, "cmd_obj": obj, "shell": shell})
        thread.daemon = True
        thread.start()
        while obj.proc is None:
            time.sleep(.001)
        OBJS_TO_CLEANUP.append(obj)
        return obj
