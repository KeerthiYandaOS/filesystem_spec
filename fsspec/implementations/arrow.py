import errno
import io
import os
import secrets
import shutil
from contextlib import suppress
from functools import wraps

from fsspec.spec import AbstractFileSystem
from fsspec.utils import infer_storage_options, mirror_from, stringify_path


def wrap_exceptions(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except OSError as exception:
            if not exception.args:
                raise

            message, *args = exception.args
            if isinstance(message, str) and "does not exist" in message:
                raise FileNotFoundError(errno.ENOENT, message) from exception
            else:
                raise

    return wrapper


class ArrowFSWrapper(AbstractFileSystem):
    """FSSpec-compatible wrapper of pyarrow.fs.FileSystem.

    Parameters
    ----------
    fs : pyarrow.fs.FileSystem

    """

    root_marker = "/"

    def __init__(self, fs, **kwargs):
        self.fs = fs
        super().__init__(**kwargs)

    @classmethod
    def _strip_protocol(cls, path):
        path = stringify_path(path)
        if "://" in path:
            _, _, path = path.partition("://")

        return path

    def ls(self, path, detail=False, **kwargs):
        from pyarrow.fs import FileSelector

        entries = [
            self._make_entry(entry)
            for entry in self.fs.get_file_info(FileSelector(path))
        ]
        if detail:
            return entries
        else:
            return [entry["name"] for entry in entries]

    def info(self, path, **kwargs):
        path = self._strip_protocol(path)
        [info] = self.fs.get_file_info([path])
        return self._make_entry(info)

    def exists(self, path):
        path = self._strip_protocol(path)
        try:
            self.info(path)
        except FileNotFoundError:
            return False
        else:
            return True

    def _make_entry(self, info):
        from pyarrow.fs import FileType

        if info.type is FileType.Directory:
            kind = "directory"
        elif info.type is FileType.File:
            kind = "file"
        elif info.type is FileType.NotFound:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), info.path)
        else:
            kind = "other"

        return {
            "name": info.path,
            "size": info.size,
            "type": kind,
            "mtime": info.mtime,
        }

    @wrap_exceptions
    def cp_file(self, path1, path2, **kwargs):
        path1 = self._strip_protocol(path1).rstrip("/")
        path2 = self._strip_protocol(path2).rstrip("/")

        with self._open(path1, "rb") as lstream:
            tmp_fname = "/".join([self._parent(path2), f".tmp.{secrets.token_hex(16)}"])
            try:
                with self.open(tmp_fname, "wb") as rstream:
                    shutil.copyfileobj(lstream, rstream)
                self.fs.move(tmp_fname, path2)
            except BaseException:  # noqa
                with suppress(FileNotFoundError):
                    self.fs.delete_file(tmp_fname)
                raise

    @wrap_exceptions
    def mv(self, path1, path2, **kwargs):
        path1 = self._strip_protocol(path1).rstrip("/")
        path2 = self._strip_protocol(path2).rstrip("/")
        self.fs.move(path1, path2)

    mv_file = mv

    @wrap_exceptions
    def rm_file(self, path):
        path = self._strip_protocol(path)
        self.fs.delete_file(path)

    @wrap_exceptions
    def rm(self, path, recursive=False, maxdepth=None):
        path = self._strip_protocol(path).rstrip("/")
        if self.isdir(path):
            if recursive:
                self.fs.delete_dir(path)
            else:
                raise ValueError("Can't delete directories without recursive=False")
        else:
            self.fs.delete_file(path)

    @wrap_exceptions
    def _open(self, path, mode="rb", block_size=None, **kwargs):
        if mode == "rb":
            stream = self.fs.open_input_stream(path)
        elif mode == "wb":
            stream = self.fs.open_output_stream(path)
        else:
            raise ValueError(f"unsupported mode for Arrow filesystem: {mode!r}")

        return ArrowFile(self, stream, path, mode, block_size, **kwargs)

    @wrap_exceptions
    def mkdir(self, path, create_parents=True, **kwargs):
        path = self._strip_protocol(path)
        if create_parents:
            self.makedirs(path, exist_ok=True)
        else:
            self.fs.create_dir(path, recursive=False)

    @wrap_exceptions
    def makedirs(self, path, exist_ok=False):
        path = self._strip_protocol(path)
        self.fs.create_dir(path, recursive=True)

    @wrap_exceptions
    def rmdir(self, path):
        path = self._strip_protocol(path)
        self.fs.delete_dir(path)


@mirror_from(
    "stream", ["read", "seek", "tell", "write", "readable", "writable", "close"]
)
class ArrowFile(io.IOBase):
    def __init__(self, fs, stream, path, mode, block_size=None, **kwargs):
        self.path = path
        self.mode = mode

        self.fs = fs
        self.stream = stream

        self.blocksize = self.block_size = block_size
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.close()


class HadoopFileSystemWrapper(ArrowFSWrapper):
    """A wrapper on top of the pyarrow.fs.HadoopFileSystem
    to connect it's interface with fsspec"""

    protocol = "hdfs"

    def __init__(
        self,
        host="default",
        port=0,
        user=None,
        kerb_ticket=None,
        extra_conf=None,
        **kwargs,
    ):
        """

        Parameters
        ----------
        host: str
            Hostname, IP or "default" to try to read from Hadoop config
        port: int
            Port to connect on, or default from Hadoop config if 0
        user: str or None
            If given, connect as this username
        kerb_ticket: str or None
            If given, use this ticket for authentication
        extra_conf: None or dict
            Passed on to HadoopFileSystem
        """
        from pyarrow.fs import HadoopFileSystem

        fs = HadoopFileSystem(
            host=host,
            port=port,
            user=user,
            kerb_ticket=kerb_ticket,
            extra_conf=extra_conf,
        )
        super().__init__(fs=fs, **kwargs)

    @staticmethod
    def _get_kwargs_from_urls(path):
        ops = infer_storage_options(path)
        out = {}
        if ops.get("host", None):
            out["host"] = ops["host"]
        if ops.get("username", None):
            out["user"] = ops["username"]
        if ops.get("port", None):
            out["port"] = ops["port"]
        return out
