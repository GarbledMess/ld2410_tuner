"""Long fits run at the lowest priority and must not delay saving what is being recorded."""

import asyncio

import real_shape
from tuner_under_test.history import dayfile
from tuner_under_test.history.daystore import bounds, date_of
from tuner_under_test.worker.supervisor import WorkerFailed


def test_a_long_learn_does_not_hold_up_saving_recordings(play):
    async def scenario(home):
        order = []
        first_running, let_go = asyncio.Event(), asyncio.Event()

        async def watch(request):
            operation = request.get("operation")
            order.append(operation)
            if operation != "learn":
                return
            if len(order) == 1:
                first_running.set()
                await let_go.wait()
            raise WorkerFailed(-9, b"stopped by the test: only the order matters")

        home.transport.before = watch
        await home.ws("start_learning", device_id="hall_1")
        await asyncio.wait_for(first_running.wait(), 30)

        home.clock.moved(2)
        home.report(real_shape.LONG_RADAR, "g1_move", 41)
        home.runtime._schedule_flush()
        await home.flushed()
        # Saved while the Learn is still running, without waiting for any worker.
        assert not any(job.done() for job in home.runtime._learning_jobs.values())
        days = home.runtime._days
        assert not days._live[real_shape.LONG_RADAR].buffer
        date = date_of(real_shape.NOW)
        logged = dayfile.decode_log(
            days._path(real_shape.LONG_RADAR, date, ".log").read_bytes(), bounds(date)[0]
        )
        assert any(getattr(record, "values", {}).get(2) == 41 for record in logged)
        assert order == ["learn"]
        let_go.set()
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)

    play(scenario)
