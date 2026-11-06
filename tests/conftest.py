"""The real-shaped home, built once per test session and copied for each test that uses it."""

import time

import pytest
import real_home
import real_shape


@pytest.fixture(scope="session")
def home_template(tmp_path_factory):
    """Built once per run: the first test worker to arrive builds, the others wait for it."""
    shared = tmp_path_factory.getbasetemp().parent / "real_home_template"
    done = shared / "config"
    try:
        shared.mkdir()
    except FileExistsError:
        deadline = time.monotonic() + 300
        while not done.exists():
            assert time.monotonic() < deadline, "Another test worker never built the home"
            time.sleep(0.1)
        return done
    partial = real_shape.build(shared / "building")
    partial.rename(done)
    return done


@pytest.fixture
def play(home_template, tmp_path):
    """``play(scenario)``: run an async ``scenario(home)`` on a private copy of the running home,
    from Home Assistant starting the integration to it unloading it."""

    def run(scenario, *, start=True, stop=True, prepare=None, whole=False):
        home = real_home.open_home(home_template, tmp_path / "config", whole=whole)
        if prepare is not None:
            prepare(home)

        async def session():
            if start:
                await home.start()
            try:
                await scenario(home)
            finally:
                if stop and home.hass is not None and real_home.DOMAIN in home.hass.data:
                    await home.stop()

        with real_home.worker_on_transport(home):
            real_home.run_on_clock(home.clock, session)
        return home

    return run
