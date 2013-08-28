"""peep ("prudently examine every package") verifies that packages conform to a
trusted, locally stored hash and only then installs them::

    peep install -r requirements.txt

This makes your deployments verifiably repeatable without having to maintain a
local PyPI mirror or use a vendor lib. Just update the version numbers and
hashes in requirements.txt, and you're all set.

"""
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from hashlib import sha256
from itertools import chain
from linecache import getline
from os import listdir
from os.path import join, basename
import re
import shlex
from shutil import rmtree
from sys import argv, exit
from tempfile import mkdtemp

import pip
from pip.log import logger
from pip.req import parse_requirements


class PipException(Exception):
    """When I delegated to pip, it exited with an error."""

    def __init__(self, error_code):
        self.error_code = error_code


def encoded_hash(sha):
    """Return a short, 7-bit-safe representation of a hash.

    If you pass a sha256, this results in the hash algorithm that the Wheel
    format (PEP 427) uses, except here it's intended to be run across the
    downloaded archive before unpacking.

    """
    return urlsafe_b64encode(sha.digest()).rstrip('=')


@contextmanager
def ephemeral_dir():
    dir = mkdtemp(prefix='peep-')
    try:
        yield dir
    finally:
        rmtree(dir)


def run_pip(initial_args):
    """Delegate to pip the given args (starting with the subcommand), and raise
    ``PipException`` if something goes wrong."""
    status_code = pip.main(initial_args=initial_args)

    # Clear out the registrations in the pip "logger" singleton. Otherwise,
    # loggers keep getting appended to it with every run. Pip assumes only one
    # command invocation will happen per interpreter lifetime.
    logger.consumers = []

    if status_code:
        raise PipException(status_code)


def pip_download(req, argv, temp_path):
    """Download a package, and return its filename.

    :arg req: The InstallRequirement which describes the package
    :arg argv: Arguments to be passed along to pip
    :arg temp_path: The path to the directory to download to

    """
    # Get the original line out of the reqs file:
    line = getline(*requirements_path_and_line(req))

    # Copy and strip off binary name. Remove any requirement file args.
    argv = ([argv[1]] +  # "install"
            ['--no-deps', '--download', temp_path] +
            list(requirement_args(argv[2:], want_other=True)) +  # other args
            shlex.split(line))  # ['nose==1.3.0']. split() removes trailing \n.

    # Remember what was in the dir so we can backtrack and tell what we've
    # downloaded (disgusting):
    old_contents = set(listdir(temp_path))

    # pip downloads the tarball into a second temp dir it creates, then it
    # copies it to our specified download dir, then it unpacks it into the
    # build dir in the venv (probably to read metadata out of it), then it
    # deletes that. Don't be afraid: the tarball we're hashing is the pristine
    # one downloaded from PyPI, not a fresh tarring of unpacked files.
    run_pip(argv)

    return (set(listdir(temp_path)) - old_contents).pop()


def pip_install_archives_from(temp_path):
    """pip install the archives from the ``temp_path`` dir, omitting
    dependencies."""
    # TODO: Make this preserve any pip options passed in, but strip off -r
    # options and other things that don't make sense at this point in the
    # process.
    for filename in listdir(temp_path):
        archive_path = join(temp_path, filename)
        run_pip(['install', '--no-deps', archive_path])


def hash_of_file(path):
    """Return the hash of a downloaded file."""
    with open(path, 'r') as archive:
        sha = sha256()
        while True:
            data = archive.read(2 ** 20)
            if not data:
                break
            sha.update(data)
    return encoded_hash(sha)


def version_of_archive(filename, package_name):
    """Deduce the version number of a downloaded package from its filename."""
    # Since we know the project_name, we can strip that off the left, strip any
    # archive extensions off the right, and take the rest as the version.
    extensions = ['.tar.gz', '.tgz', '.tar', '.zip']
    for ext in extensions:
        if filename.endswith(ext):
            filename = filename[:-len(ext)]
            break
    if not filename.startswith(package_name):
        # TODO: What about safe/unsafe names?
        raise RuntimeError("The archive '%s' didn't start with the package name '%s', so I couldn't figure out the version number. My bad; improve me.")
    return filename[len(package_name) + 1:]  # Strip off '-' before version.


def requirement_args(argv, want_paths=False, want_other=False):
    """Return an iterable of filtered arguments.
    
    :arg want_paths: If True, the returned iterable includes the paths to any
        requirements files following a ``-r`` or ``--requirement`` option.
    :arg want_other: If True, the returned iterable includes the args that are
        not a requirement-file path or a ``-r`` or ``--requirement`` flag.
    
    """
    was_r = False
    for arg in argv:
        # Allow for requirements files named "-r", don't freak out if there's a
        # trailing "-r", etc.
        if was_r:
            if want_paths:
                yield arg
            was_r = False
        elif arg in ['-r', '--requirement']:
            was_r = True
        else:
            if want_other:
                yield arg


def requirements_path_and_line(req):
    """Return the path and line number of the file from which an
    InstallRequirement came."""
    path, line = (re.match(r'-r (.*) \(line (\d+)\)$',
                  req.comes_from).groups())
    return path, int(line)


def hashes_of_requirements(requirements):
    """Return a map of package names to expected hashes, given multiple
    requirements files."""
    expected_hashes = {}
    missing_hashes = []

    for req in requirements:  # InstallRequirements
        path, line_number = requirements_path_and_line(req)
        if line_number > 1:
            previous_line = getline(path, line_number - 1)
            if previous_line.startswith('# sha256: '):
                expected_hashes[req.name] = previous_line.split(':', 1)[1].strip()
                continue
        missing_hashes.append(req.name)
    return expected_hashes, missing_hashes


def hash_mismatches(expected_hashes, downloaded_hashes):
    """Yield the expected hash, package name, and download-hash of each
    package whose download-hash didn't match the one specified for it in the
    requirments file."""
    for package_name, expected_hash in expected_hashes.iteritems():
        hash_of_download = downloaded_hashes[package_name]
        if hash_of_download != expected_hash:
            yield expected_hash, package_name, hash_of_download


def main():
    """Implement "peep install". Return a shell status code."""
    ITS_FINE_ITS_FINE = 0
    SOMETHING_WENT_WRONG = 1
    # "Traditional" for command-line errors according to optparse docs:
    COMMAND_LINE_ERROR = 2

    try:
        if not (len(argv) >= 2 and argv[1] == 'install'):
            # Fall through to top-level pip main() for everything else:
            return pip.main()

        req_paths = list(requirement_args(argv[1:], want_paths=True))
        if not req_paths:
            print "You have to specify one or more requirements files with the -r option, because otherwise there's nowhere for peep to look up the hashes."
            return COMMAND_LINE_ERROR

        # We're a "peep install" command, and we have some requirement paths.

        requirements = list(chain(*(parse_requirements(path) for
                                    path in req_paths)))
        downloaded_hashes, downloaded_versions = {}, {}
        with ephemeral_dir() as temp_path:
            for req in requirements:
                name = req.req.project_name
                archive_filename = pip_download(req, argv, temp_path)
                downloaded_hashes[name] = hash_of_file(join(temp_path, archive_filename))
                downloaded_versions[name] = version_of_archive(archive_filename, name)

            expected_hashes, missing_hashes = hashes_of_requirements(requirements)
            mismatches = list(hash_mismatches(expected_hashes, downloaded_hashes))

            # Skip a line after pip's "Cleaning up..." so the important stuff
            # stands out:
            if mismatches or missing_hashes:
                print

            # Mismatched hashes:
            if mismatches:
                print "THE FOLLOWING PACKAGES DIDN'T MATCHES THE HASHES SPECIFIED IN THE REQUIREMENTS FILE. If you have updated the package versions, update the hashes. If not, freak out, because someone has tampered with the packages.\n"
            for expected_hash, package_name, hash_of_download in mismatches:
                hash_of_download = downloaded_hashes[package_name]
                if hash_of_download != expected_hash:
                    print '    %s: expected %s' % (
                            package_name,
                            expected_hash)
                    print ' ' * (5 + len(package_name)), '     got', hash_of_download
            if mismatches:
                print  # Skip a line before "Not proceeding..."

            # Missing hashes:
            if missing_hashes:
                print 'The following packages had no hashes specified in the requirements file, which leaves them open to tampering. Vet these packages to your satisfaction, then add these "sha256" lines like so:\n'
            for package_name in missing_hashes:
                print '# sha256: %s' % downloaded_hashes[package_name]
                print '%s==%s\n' % (package_name,
                                    downloaded_versions[package_name])

            if mismatches or missing_hashes:
                print '-------------------------------'
                print 'Not proceeding to installation.'
                return SOMETHING_WENT_WRONG
            else:
                pip_install_archives_from(temp_path)
    except PipException as exc:
        return exc.error_code
    return ITS_FINE_ITS_FINE