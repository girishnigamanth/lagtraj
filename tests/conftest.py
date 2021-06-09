from pathlib import Path
import tarfile
import tempfile
import os
import shutil

import requests
import pytest

import lagtraj.domain.load
from make_test_data import TEST_FORCING_NAME, trajectory_load, forcing_load


TESTDATA_URL = "http://gws-access.jasmin.ac.uk/public/eurec4auk/testdata/lagtraj.testdata.v2.tar.gz"  # noqa

if os.environ.get("LAGTRAJ_TESTDATA_DIR", None):
    TESTDATA_DIR = Path(os.environ["LAGTRAJ_TESTDATA_DIR"])
else:
    tempdir = tempfile.TemporaryDirectory()
    TESTDATA_DIR = Path(tempdir.name)


def _download_testdata():
    fhtar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")

    r = requests.get(TESTDATA_URL)
    fhtar.write(r.content)
    fhtar.close()

    tarfile.open(fhtar.name, "r:gz").extractall(TESTDATA_DIR)


def ensure_testdata_available():
    if not TESTDATA_DIR.exists():
        raise Exception(f"Couldn't find test-data directory {TESTDATA_DIR}")
    # Download testdata if it is not there yet
    if len(list(TESTDATA_DIR.glob("**/*.nc"))) == 0:
        print("Downloading testdata...")
        _download_testdata()

    # the testdata directory should only contain the domain data anothing else
    testdata_dir_contents = TESTDATA_DIR.glob("*")
    to_delete = [p for p in testdata_dir_contents if p.name != "domains"]
    for path_to_delete in to_delete:
        shutil.rmtree(path_to_delete)


@pytest.fixture
def ds_domain_test(scope="session"):
    ensure_testdata_available()
    DOMAIN_NAME = "eurec4a_circle"
    ds = lagtraj.domain.load.load_data(root_data_path=TESTDATA_DIR, name=DOMAIN_NAME)
    return ds


@pytest.fixture
def ds_trajectory_linear(ds_domain_test):
    t0 = ds_domain_test.time.isel(time=-15)

    origin = lagtraj.trajectory.TrajectoryOrigin(
        lat=ds_domain_test.lat.mean(), lon=ds_domain_test.lon.mean(), datetime=t0,
    )

    da_times = ds_domain_test.time

    ds_traj = lagtraj.trajectory.create.create_trajectory(
        origin=origin, trajectory_type="linear", da_times=da_times, U=[0.0, -0.0]
    )

    return ds_traj


@pytest.fixture
def testdata_info():
    """
    These are used for the CLI tests. We might want to add input definitions to
    the testdata (see `make_test_data.py`) and test more CLI calls in future.
    """
    ensure_testdata_available()
    p_root = Path(TESTDATA_DIR)
    forcing_name = TEST_FORCING_NAME
    forcing_defn = forcing_load.load_definition(p_root, forcing_name=forcing_name)
    trajectory_name = forcing_defn.name
    trajectory_defn = trajectory_load.load_definition(p_root, name=trajectory_name)
    domain_name = trajectory_defn.domain

    return dict(
        testdata_path=p_root,
        forcing_name=forcing_name,
        trajectory_name=trajectory_name,
        domain_name=domain_name,
    )
