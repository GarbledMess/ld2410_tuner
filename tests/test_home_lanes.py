"""Long fits run at the lowest priority and must not delay saving what is being recorded."""

import asyncio

import real_shape
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
        assert order[:2] == ["learn", "flush"]  # Saved while the Learn is still running.
        let_go.set()
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)

    play(scenario)
