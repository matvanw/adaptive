# -*- coding: utf-8 -*-
import asyncio
import functools
import inspect
import concurrent.futures as concurrent
import warnings

try:
    import ipyparallel
    with_ipyparallel = True
except ModuleNotFoundError:
    with_ipyparallel = False

try:
    import distributed
    with_distributed = True
except ModuleNotFoundError:
    with_distributed = False

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ModuleNotFoundError:
    pass


class BaseRunner:
    """Base class for runners that use concurrent.futures.Executors.

    Parameters
    ----------
    learner : adaptive.learner.BaseLearner
    goal : callable
        The end condition for the calculation. This function must take
        the learner as its sole argument, and return True when we should
        stop requesting more points.
    executor : concurrent.futures.Executor, or ipyparallel.Client, optional
        The executor in which to evaluate the function to be learned.
        If not provided, a new ProcessPoolExecutor is used.
    ntasks : int, optional
        The number of concurrent function evaluations. Defaults to the number
        of cores available in 'executor'.
    log : bool, default: False
        If True, record the method calls made to the learner by this runner
    shutdown_executor : Bool, default: True
        If True, shutdown the executor when the runner has completed. If
        'executor' is not provided then the executor created internally
        by the runner is shut down, regardless of this parameter.

    Attributes
    ----------
    learner : Learner
        The underlying learner. May be queried for its state
    log : list or None
        Record of the method calls made to the learner, in the format
        '(method_name, *args)'.
    """

    def __init__(self, learner, goal, *,
                 executor=None, ntasks=None, log=False,
                 shutdown_executor=True):

        self.executor = _ensure_executor(executor)
        self.goal = goal

        self.ntasks = ntasks or _get_ncores(self.executor)
        # if we instantiate our own executor, then we are also responsible
        # for calling 'shutdown'
        self.shutdown_executor = shutdown_executor or (executor is None)

        self.learner = learner
        self.log = [] if log else None
        self.task = None


class BlockingRunner(BaseRunner):
    """Run a learner synchronously in an executor.

    Parameters
    ----------
    learner : adaptive.learner.BaseLearner
    goal : callable
        The end condition for the calculation. This function must take
        the learner as its sole argument, and return True when we should
        stop requesting more points.
    executor : concurrent.futures.Executor, or ipyparallel.Client, optional
        The executor in which to evaluate the function to be learned.
        If not provided, a new ProcessPoolExecutor is used.
    ntasks : int, optional
        The number of concurrent function evaluations. Defaults to the number
        of cores available in 'executor'.
    log : bool, default: False
        If True, record the method calls made to the learner by this runner
    shutdown_executor : Bool, default: True
        If True, shutdown the executor when the runner has completed. If
        'executor' is not provided then the executor created internally
        by the runner is shut down, regardless of this parameter.

    Attributes
    ----------
    learner : Learner
        The underlying learner. May be queried for its state
    log : list or None
        Record of the method calls made to the learner, in the format
        '(method_name, *args)'.
    """

    def __init__(self, learner, goal, *,
                 executor=None, ntasks=None, log=False,
                 shutdown_executor=True):
        if inspect.iscoroutinefunction(learner.function):
            raise ValueError("Coroutine functions can only be used "
                             "with 'AsyncRunner'.")
        super().__init__(learner, goal, executor=executor, ntasks=ntasks,
                         log=log, shutdown_executor=shutdown_executor)
        self._run()

    def _submit(self, x):
        return self.executor.submit(self.learner.function, x)

    def _run(self):
        first_completed = concurrent.FIRST_COMPLETED
        xs = dict()
        done = [None] * self.ntasks
        do_log = self.log is not None

        if len(done) == 0:
            raise RuntimeError('Executor has no workers')

        try:
            while not self.goal(self.learner):
                # Launch tasks to replace the ones that completed
                # on the last iteration.
                if do_log:
                    self.log.append(('choose_points', len(done)))

                points, _ = self.learner.choose_points(len(done))
                for x in points:
                    xs[self._submit(x)] = x

                # Collect and results and add them to the learner
                futures = list(xs.keys())
                done, _ = concurrent.wait(futures,
                                          return_when=first_completed)
                for fut in done:
                    x = xs.pop(fut)
                    y = fut.result()
                    if do_log:
                        self.log.append(('add_point', x, y))
                    self.learner.add_point(x, y)

        finally:
            # remove points with 'None' values from the learner
            self.learner.remove_unfinished()
            # cancel any outstanding tasks
            remaining = list(xs.keys())
            if remaining:
                for fut in remaining:
                    fut.cancel()
                concurrent.wait(remaining)
            if self.shutdown_executor:
                self.executor.shutdown()


class AsyncRunner(BaseRunner):
    """Run a learner asynchronously in an executor using asyncio.

    This runner assumes that



    Parameters
    ----------
    learner : adaptive.learner.BaseLearner
    goal : callable, optional
        The end condition for the calculation. This function must take
        the learner as its sole argument, and return True when we should
        stop requesting more points. If not provided, the runner will run
        forever, or until 'self.task.cancel()' is called.
    executor : concurrent.futures.Executor, or ipyparallel.Client, optional
        The executor in which to evaluate the function to be learned.
        If not provided, a new ProcessPoolExecutor is used.
    ntasks : int, optional
        The number of concurrent function evaluations. Defaults to the number
        of cores available in 'executor'.
    log : bool, default: False
        If True, record the method calls made to the learner by this runner
    shutdown_executor : Bool, default: True
        If True, shutdown the executor when the runner has completed. If
        'executor' is not provided then the executor created internally
        by the runner is shut down, regardless of this parameter.
    ioloop : asyncio.AbstractEventLoop, optional
        The ioloop in which to run the learning algorithm. If not provided,
        the default event loop is used.

    Attributes
    ----------
    task : asyncio.Task
        The underlying task. May be cancelled in order to stop the runner.
    learner : Learner
        The underlying learner. May be queried for its state.
    log : list or None
        Record of the method calls made to the learner, in the format
        '(method_name, *args)'.

    Notes
    -----
    This runner can be used when an async function (defined with
    'async def') has to be learned. In this case the function will be
    run directly on the event loop (and not in the executor).
    """

    def __init__(self, learner, goal=None, *,
                 executor=None, ntasks=None, log=False,
                 ioloop=None, shutdown_executor=True):

        if goal is None:
            def goal(_):
                return False

        super().__init__(learner, goal, executor=executor, ntasks=ntasks,
                         log=log, shutdown_executor=shutdown_executor)
        self.ioloop = ioloop or asyncio.get_event_loop()
        self.task = None

        # When the learned function is 'async def', we run it
        # directly on the event loop, and not in the executor.
        if inspect.iscoroutinefunction(learner.function):
            if executor:  # what the user provided
                raise RuntimeError('Executor is unused when learning '
                                   'an async function')
            self.executor.shutdown()  # Make sure we don't shoot ourselves later

            self._submit = lambda x: self.ioloop.create_task(learner.function(x))
        else:
            self._submit = functools.partial(self.ioloop.run_in_executor,
                                             self.executor,
                                             self.learner.function)

        if in_ipynb() and not self.ioloop.is_running():
            warnings.warn('Run adaptive.notebook_extension() to use '
                          'the Runner in a Jupyter notebook.')
        self.task = self.ioloop.create_task(self._run())

    async def _run(self):
        first_completed = asyncio.FIRST_COMPLETED
        xs = dict()
        done = [None] * self.ntasks
        do_log = self.log is not None

        if len(done) == 0:
            raise RuntimeError('Executor has no workers')

        try:
            while not self.goal(self.learner):
                # Launch tasks to replace the ones that completed
                # on the last iteration.
                if do_log:
                    self.log.append(('choose_points', len(done)))

                points, _ = self.learner.choose_points(len(done))
                for x in points:
                    xs[self._submit(x)] = x

                # Collect and results and add them to the learner
                futures = list(xs.keys())
                done, _ = await asyncio.wait(futures,
                                             return_when=first_completed,
                                             loop=self.ioloop)
                for fut in done:
                    x = xs.pop(fut)
                    y = fut.result()
                    if do_log:
                        self.log.append(('add_point', x, y))
                    self.learner.add_point(x, y)
        finally:
            # remove points with 'None' values from the learner
            self.learner.remove_unfinished()
            # cancel any outstanding tasks
            remaining = list(xs.keys())
            if remaining:
                for fut in remaining:
                    fut.cancel()
                await asyncio.wait(remaining)
            if self.shutdown_executor:
                self.executor.shutdown()


# Default runner
Runner = AsyncRunner


def replay_log(learner, log):
    """Apply a sequence of method calls to a learner.

    This is useful for debugging runners.

    Parameters
    ----------
    learner : learner.BaseLearner
    log : list
        contains tuples: '(method_name, *args)'.
    """
    for method, *args in log:
        getattr(learner, method)(*args)


class SequentialExecutor(concurrent.Executor):
    """A trivial executor that runs functions synchronously.

    This executor is mainly for testing.
    """
    def submit(self, fn, *args, **kwargs):
        fut = concurrent.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as e:
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterable, timeout=None, chunksize=1):
        return map(fn, iterable)

    def shutdown(self, wait=True):
        pass


def _ensure_executor(executor):
    if executor is None:
        return concurrent.ProcessPoolExecutor()
    elif isinstance(executor, concurrent.Executor):
        return executor
    elif with_ipyparallel and isinstance(executor, ipyparallel.Client):
        return executor.executor()
    elif with_distributed and isinstance(executor, distributed.Client):
        return executor.get_executor()
    else:
        raise TypeError('Only a concurrent.futures.Executor, distributed.Client,'
                        ' or ipyparallel.Client can be used.')


def _get_ncores(ex):
    """Return the maximum  number of cores that an executor can use."""
    if with_ipyparallel and isinstance(ex, ipyparallel.client.view.ViewExecutor):
        return len(ex.view)
    elif isinstance(ex, (concurrent.ProcessPoolExecutor,
                         concurrent.ThreadPoolExecutor)):
        return ex._max_workers  # not public API!
    elif isinstance(ex, SequentialExecutor):
        return 1
    elif with_distributed and isinstance(ex, distributed.cfexecutor.ClientExecutor):
        # XXX: check if not sum(n for n in ex._client.ncores().values())
        return len(ex._client.ncores())
    else:
        raise TypeError('Cannot get number of cores for {}'
                        .format(ex.__class__))


def in_ipynb():
    try:
        return get_ipython().__class__.__name__ == 'ZMQInteractiveShell'
    except NameError:
        return False
