from threading import Event, Thread, Lock
from six.moves.queue import Queue, Empty
import time


class CachePipeline(object):
    def __init__(self, spider, cache):
        self.spider = spider
        self.cache = cache
        self.queue_size = 100
        self.input_queue = Queue()
        self.result_queue = Queue()
        self.active_tasks = 0
        self.active_tasks_lock = Lock()

        self.thread = Thread(target=self.thread_worker)
        self.thread.daemon = True
        self.thread.start()

    def has_free_resources(self):
        return (self.input_queue.qsize() < self.queue_size
                and self.result_queue.qsize() < self.queue_size)

    def is_idle(self):
        self.active_tasks_lock.acquire()
        try:
            return (self.active_tasks == 0
                    and self.input_queue.qsize() == 0
                    and self.result_queue.qsize() == 0)
        finally:
            self.active_tasks_lock.release()

    def thread_worker(self):
        while True:
            try:
                action, data = self.input_queue.get(block=False)
            except Empty:
                time.sleep(0.1)
                if self.spider.shutdown_event.is_set():
                    #print('EXITING CACHE PIPELINE')
                    return self.shutdown()
                #else:
                #    print('no shutdown event')
            else:
                try:
                    assert action in ('load', 'save')
                    if action == 'load':
                        task, grab = data
                        result = None
                        if self.is_cache_loading_allowed(task, grab):
                            result = self.load_from_cache(task, grab)
                        if result:
                            #print('!! PUT RESULT INTO CACHE PIPE RESULT QUEUE (cache)')
                            self.result_queue.put(('network_result', result))
                        else:
                            self.result_queue.put(('task', task))
                    elif action == 'save':
                        task, grab = data
                        if self.is_cache_saving_allowed(task, grab):
                            with self.spider.timer.log_time('cache'):
                                with self.spider.timer.log_time('cache.write'):
                                    self.cache.save_response(task.url, grab)
                finally:
                    self.active_tasks_lock.acquire()
                    try:
                        self.active_tasks -= 1
                    finally:
                        self.active_tasks_lock.release()

    def is_cache_loading_allowed(self, task, grab):
        # 1) cache data should be refreshed
        # 2) cache is disabled for that task
        # 3) request type is not cacheable
        return (not task.get('refresh_cache', False)
                and not task.get('disable_cache', False)
                and grab.detect_request_method() == 'GET')

    def is_cache_saving_allowed(self, task, grab):
        """
        Check if network transport result could
        be saved to cache layer.

        res: {ok, grab, grab_config_backup, task, emsg}
        """

        if grab.request_method == 'GET':
            if not task.get('disable_cache'):
                if self.spider.is_valid_network_response_code(
                        grab.response.code, task):
                    return True
        return False


    def load_from_cache(self, task, grab):
        with self.spider.timer.log_time('cache'):
            with self.spider.timer.log_time('cache.read'):
                cache_item = self.cache.get_item(
                    grab.config['url'], timeout=task.cache_timeout)
                if cache_item is None:
                    return None
                else:
                    with self.spider.timer.log_time('cache.read.prepare_request'):
                        grab.prepare_request()
                    with self.spider.timer.log_time('cache.read.load_response'):
                        self.cache.load_response(grab, cache_item)

                    grab.log_request('CACHED')
                    self.spider.stat.inc('spider:request-cache')

                    return {'ok': True, 'task': task, 'grab': grab,
                            'grab_config_backup': grab.dump_config(),
                            'emsg': None}

    def shutdown(self):
        try:
            self.cache.close()
        except AttributeError:
            print('Cache %s does not support close method' % self.cache)

    def get_ready_results(self):
        res = []
        while True:
            try:
                action, result = self.result_queue.get(block=False)
            except Empty:
                break
            else:
                res.append((action, result))
                assert action in ('network_result', 'task')
        return res

    def add_task(self, task):
        self.input_queue.put(task)
        self.active_tasks_lock.acquire()
        try:
            self.active_tasks += 1
        finally:
            self.active_tasks_lock.release()
