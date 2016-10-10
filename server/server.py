# See the file "LICENSE" for information about the copyright
# and warranty status of this software.

import asyncio
import json
import logging
import os
import signal
import time
from functools import partial

import aiohttp

from server.db import DB


class Server(object):

    def __init__(self, env):
        self.env = env
        self.db = DB(env)
        self.rpc = RPC(env)
        self.block_cache = BlockCache(env, self.db, self.rpc)

    def async_tasks(self):
        return [
            asyncio.ensure_future(self.block_cache.catch_up()),
            asyncio.ensure_future(self.block_cache.process_cache()),
        ]


class BlockCache(object):
    '''Requests blocks ahead of time from the daemon.  Serves them
    to the blockchain processor.'''

    def __init__(self, env, db, rpc):
        self.logger = logging.getLogger('BlockCache')
        self.logger.setLevel(logging.INFO)

        self.db = db
        self.rpc = rpc
        self.stop = False
        # Cache target size is in MB.  Has little effect on sync time.
        self.cache_limit = 10
        self.daemon_height = 0
        self.fetched_height = db.db_height
        # Blocks stored in reverse order.  Next block is at end of list.
        self.blocks = []
        self.recent_sizes = []
        self.ave_size = 0

        loop = asyncio.get_event_loop()
        for signame in ('SIGINT', 'SIGTERM'):
            loop.add_signal_handler(getattr(signal, signame),
                                    partial(self.on_signal, signame))

    def on_signal(self, signame):
        logging.warning('Received {} signal, preparing to shut down'
                        .format(signame))
        self.blocks = []
        self.stop = True

    async def process_cache(self):
        while not self.stop:
            await asyncio.sleep(1)
            while self.blocks:
                self.db.process_block(self.blocks.pop())
                # Release asynchronous block fetching
                await asyncio.sleep(0)

        self.db.flush()

    async def catch_up(self):
        self.logger.info('catching up, block cache limit {:d}MB...'
                         .format(self.cache_limit))

        last_log = 0
        prior_height = self.db.height
        while await self.maybe_prefill():
            now = time.time()
            count = self.fetched_height - prior_height
            if now > last_log + 15 and count:
                last_log = now
                prior_height = self.fetched_height
                self.logger.info('prefilled {:,d} blocks to height {:,d} '
                                 'daemon height: {:,d}'
                                 .format(count, self.fetched_height,
                                         self.daemon_height))
            await asyncio.sleep(1)

        if not self.stop:
            self.logger.info('caught up to height {:d}'
                             .format(self.daemon_height))

    def cache_used(self):
        return sum(len(block) for block in self.blocks)

    def prefill_count(self, room):
        count = 0
        if self.ave_size:
            count = room // self.ave_size
        return max(count, 10)

    async def maybe_prefill(self):
        '''Returns False to stop.  True to sleep a while for asynchronous
        processing.'''
        cache_limit = self.cache_limit * 1024 * 1024
        while True:
            if self.stop:
                return False

            cache_used = self.cache_used()
            if cache_used > cache_limit:
                return True

            # Keep going by getting a whole new cache_limit of blocks
            self.daemon_height = await self.rpc.rpc_single('getblockcount')
            max_count = min(self.daemon_height - self.fetched_height, 4000)
            count = min(max_count, self.prefill_count(cache_limit))
            if not count or self.stop:
                return False  # Done catching up

            first = self.fetched_height + 1
            param_lists = [[height] for height in range(first, first + count)]
            hashes = await self.rpc.rpc_multi('getblockhash', param_lists)

            if self.stop:
                return False

            # Hashes is an array of hex strings
            param_lists = [(h, False) for h in hashes]
            blocks = await self.rpc.rpc_multi('getblock', param_lists)
            self.fetched_height += count

            if self.stop:
                return False

            # Convert hex string to bytes and put in memoryview
            blocks = [memoryview(bytes.fromhex(block)) for block in blocks]
            # Reverse order and place at front of list
            self.blocks = list(reversed(blocks)) + self.blocks

            # Keep 50 most recent block sizes for fetch count estimation
            sizes = [len(block) for block in blocks]
            self.recent_sizes.extend(sizes)
            excess = len(self.recent_sizes) - 50
            if excess > 0:
                self.recent_sizes = self.recent_sizes[excess:]
            self.ave_size = sum(self.recent_sizes) // len(self.recent_sizes)


class RPC(object):

    def __init__(self, env):
        self.logger = logging.getLogger('RPC')
        self.logger.setLevel(logging.INFO)
        self.rpc_url = env.rpc_url
        self.logger.info('using RPC URL {}'.format(self.rpc_url))

    async def rpc_multi(self, method, param_lists):
        payload = [{'method': method, 'params': param_list}
                   for param_list in param_lists]
        while True:
            dresults = await self.daemon(payload)
            errs = [dresult['error'] for dresult in dresults]
            if not any(errs):
                return [dresult['result'] for dresult in dresults]
            for err in errs:
                if err.get('code') == -28:
                    self.logger.warning('daemon still warming up...')
                    secs = 10
                    break
            else:
                self.logger.error('daemon returned errors: {}'.format(errs))
                secs = 0
            self.logger.info('sleeping {:d} seconds and trying again...'
                             .format(secs))
            await asyncio.sleep(secs)


    async def rpc_single(self, method, params=None):
        payload = {'method': method}
        if params:
            payload['params'] = params
        while True:
            dresult = await self.daemon(payload)
            err = dresult['error']
            if not err:
                return dresult['result']
            if err.get('code') == -28:
                self.logger.warning('daemon still warming up...')
                secs = 10
            else:
                self.logger.error('daemon returned error: {}'.format(err))
                secs = 0
            self.logger.info('sleeping {:d} seconds and trying again...'
                             .format(secs))
            await asyncio.sleep(secs)

    async def daemon(self, payload):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.rpc_url,
                                            data=json.dumps(payload)) as resp:
                        return await resp.json()
            except Exception as e:
                self.logger.error('aiohttp error: {}'.format(e))

            self.logger.info('sleeping 1 second and trying again...')
            await asyncio.sleep(1)