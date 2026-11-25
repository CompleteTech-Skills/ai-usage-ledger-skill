"""Read plain or compressed agent logs without extracting them to disk."""

import contextlib
import gzip
import os
import shutil
import subprocess

try:
    from compression import zstd
except ImportError:  # Python before 3.14 can use the installed zstd command.
    zstd = None


def uncompressed_name(path):
    name = os.path.basename(path)
    for suffix in (".gz", ".zst", ".zstd"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


@contextlib.contextmanager
def open_lines(path):
    if str(path).endswith(".gz"):
        try:
            with gzip.open(path, "rb") as stream:
                yield stream
        except (EOFError, gzip.BadGzipFile) as error:
            raise OSError("invalid or incomplete gzip stream") from error
    elif str(path).endswith((".zst", ".zstd")):
        if zstd is not None:
            try:
                with zstd.open(path, "rb") as stream:
                    yield stream
            except (EOFError, zstd.ZstdError) as error:
                raise OSError("invalid or incomplete Zstandard stream") from error
        else:
            executable = shutil.which("zstd")
            if not executable:
                raise OSError("Zstandard input requires Python 3.14+ or the zstd executable")
            with subprocess.Popen([executable, "-d", "-q", "-c", "--", os.fspath(path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
                try:
                    with decoder.stdout as stream:
                        yield stream
                except BaseException:
                    decoder.terminate()
                    raise
                if decoder.wait() != 0:
                    raise OSError("zstd could not decode the stream")
    else:
        with open(path, "rb") as stream:
            yield stream
