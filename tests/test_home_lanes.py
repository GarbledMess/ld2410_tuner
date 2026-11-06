"""Who waits for whom on the worker: what the user asked for goes ahead of routine saving."""

import asyncio

import real_shape
from tuner_under_test.worker.supervisor import WorkerFailed


def test_a_learn_the_user_starts_overtakes_a_routine_flush_waiting_for_the_worker(play):
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
        await asyncio.sleep(0.2)  # The flush is now queued behind the running job.
        await home.ws("start_learning", device_id="guest_room_1")
        await asyncio.sleep(0.2)
        let_go.set()
        await asyncio.gather(*home.runtime._learning_jobs.values(), return_exceptions=True)
        await home.flushed()
        assert order[:3] == ["learn", "learn", "flush"]

    play(scenario)
