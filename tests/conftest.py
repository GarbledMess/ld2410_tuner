"""The real-shaped home, built once per test session and copied for each test that uses it."""

import pytest
import real_home
import real_shape


@pytest.fixture(scope="session")
def home_template(tmp_path_factory):
    return real_shape.build(tmp_path_factory.mktemp("real_home_template") / "config")


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
