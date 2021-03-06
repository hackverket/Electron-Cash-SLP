"""
Background search and batch download for graph transactions.

This is used by slp_validator_0x01.py.
"""

import sys
import time
import threading
import queue
import traceback
import weakref
import collections
import json
import base64
import requests
from .transaction import Transaction
from .caches import ExpiringCache

class SlpdbErrorNoSearchData(Exception):
    pass

class GraphSearchJob:
    def __init__(self, txid, valjob_ref):
        self.root_txid = txid
        self.valjob = valjob_ref

        # metadata fetched from back end
        self.depth_map = None
        self.total_depth = None
        self.txn_count_total = None

        # job status info
        self.search_started = False
        self.search_success = None
        self.job_complete = False
        self.exit_msg = None
        self.depth_completed = 0
        self.depth_current_query = None
        self.txn_count_progress = 0
        self.last_search_url = '(url empty)'

        # ctl
        self.waiting_to_cancel = False
        self.cancel_callback = None
        self.fetch_retries = 0

    def sched_cancel(self, callback=None, reason='job canceled'):
        self.exit_msg = reason
        if self.job_complete:
            return
        if not self.waiting_to_cancel:
            self.waiting_to_cancel = True
            self.cancel_callback = callback
            return

    def _cancel(self):
        self.job_complete = True
        self.search_success = False
        if self.cancel_callback:
            self.cancel_callback(self)

    def set_success(self):
        self.search_success = True
        self.job_complete = True

    def set_failed(self, reason=None):
        self.search_started = True
        self.search_success = False
        self.job_complete = True
        self.exit_msg = reason

    def fetch_metadata(self):
        try:
            res = self.metadata_query(self.root_txid, self.valjob.network.slpdb_host)
            self.total_depth = res['totalDepth']
            self.txn_count_total = res['txcount']
            self.depth_map = res['depthMap']
        except KeyError as e:
            raise SlpdbErrorNoSearchData(str(e))

    def metadata_query(self, txid, slpdb_host):
        requrl = self.metadata_url([txid], slpdb_host)
        print("[SLP Graph Search] depth search url = " + requrl, file=sys.stderr)
        reqresult = requests.get(requrl, timeout=10)
        res = dict()
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            o = { 'depthMap': resp['depthMap'], 'txcount': resp['txcount'], 'totalDepth': resp['totalDepth'] }
            res = o
        return res

    def metadata_url(self, txids, host):
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$project": {
                            "_id": 0,
                            "txid": "$graphTxn.txid",
                            "txcount": "$graphTxn.stats.txcount",
                            "totalDepth": "$graphTxn.stats.depth",
                            "depthMap": "$graphTxn.stats.depthMap"
                        }
                    }
                ],
                "limit": len(txids)
            }
        }
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        url = host + "/q/" + q.decode('utf-8')
        return url


class SlpGraphSearchManager:
    """
    A single thread that processes graph search requests sequentially.
    """
    def __init__(self, threadname="GraphSearch"):
        # holds the job history and status
        self.search_jobs = dict()
        self.lock = threading.Lock()

        # Create a single use queue on a new thread
        self.metadata_queue = queue.Queue()
        self.search_queue = queue.Queue()  # TODO: make this a PriorityQueue based on dag size

        self.threadname = threadname
        self.metadata_thread = threading.Thread(target=self.metadata_loop, name=self.threadname+'/metadata', daemon=True)
        self.metadata_thread.start()
        self.search_thread = threading.Thread(target=self.search_loop, name=self.threadname+'/search', daemon=True)
        self.search_thread.start()

        # dag size threshold to auto-cancel job
        self.cancel_thresh_txcount = 20

    def new_search(self, valjob_ref):
        """ 
        Starts a new thread to fetch GS metadata for a job. 
        Depending on the metadata results the job may end up being added to the GS queue. 
        
        Returns weakref of the new GS job object if new job is created.
        """
        txid = valjob_ref.root_txid
        with self.lock:
            job = GraphSearchJob(txid, valjob_ref)
            self.metadata_queue.put(job)
            return job
        return None

    def restart_search(self, job):
        def callback(job):
            with self.lock:
                self.search_jobs.pop(job.root_txid, None)
            self.new_search(job.valjob)
            job = None
        if not job.job_complete:
            job.sched_cancel(callback, reason='job restarted')
        else:
            callback(job)

    def metadata_loop(self):
        while True:
            job = self.metadata_queue.get(block=True)
            if job.root_txid not in self.search_jobs.keys():
                self.search_jobs[job.root_txid] = job
            else:
                continue
            try:
                if not job.valjob.running and not job.valjob.has_never_run:
                    job.set_failed('validation finished')
                    continue
                if not job.valjob.network.slpdb_host:
                    job.set_failed('SLPDB host not set')
                    continue
                job.fetch_metadata()
            except SlpdbErrorNoSearchData as e:
                if job.fetch_retries > 10:
                    job.set_failed("No data found, right-click to try")
                    continue
                if self.metadata_queue.empty():
                    time.sleep(10)  # Want to this time delay for when a brand new SLP txn comes in, gives SLPDB time to catch-up
                job.fetch_retries += 1
                self.metadata_queue.put(job)
                continue
            except Exception as e:
                print("error in graph search query", str(e), file=sys.stderr)
                job.set_failed(str(e))
                continue
            else:
                if not job.txn_count_total and job.txn_count_total != 0:
                    job.set_failed('metadata error')
                    continue
                if job.txn_count_total <= self.cancel_thresh_txcount:
                    job.set_failed('low txn count')
                    continue
                self.search_queue.put(job)
                continue
                # TODO We need to add a priority parameter for the search jobs queue based on:
                #     - sort queue by DAG size, largest jobs will benefit from GS the most.

    def search_loop(self,):
        try:
            while True:
                job = self.search_queue.get(block=True)
                job.search_started = True
                if not job.valjob.running and not job.valjob.has_never_run:
                    job.set_failed('validation finished')
                    continue
                try:
                    # search_query is a recursive call, most time will be spent here
                    self.search_query(job)
                except Exception as e:
                    print("error in graph search query", e, file=sys.stderr)
                    job.set_failed(str(e))
        finally:
            print("[Graph Search] Error: SearchGraph mainloop exited.", file=sys.stderr)

    def search_query(self, job, txids=None, depth_map_index=0):
        if job.waiting_to_cancel:
            job._cancel()
            return
        if not job.valjob.running and not job.valjob.has_never_run:
            job.set_failed('validation finished')
            return
        if depth_map_index == 0:
            txids = [job.root_txid]
        job.depth_current_query, txn_count = job.depth_map[str((depth_map_index+1)*1000)]  # currently, we query for chunks with up to 1000 txns
        if depth_map_index > 0:
            query_depth = job.depth_current_query - job.depth_map[str((depth_map_index)*1000)][0]
            txn_count = txn_count - job.depth_map[str((depth_map_index)*1000)][1]
        else:
            query_depth = job.depth_current_query
        query_json = self.get_query_json(txids, query_depth, job.valjob.network.slpdb_host) #TODO: handle 'validity_cache' exclusion from graph search (NOTE: this will impact total dl count)
        job.last_search_url = job.valjob.network.slpdb_host + "/q/" + base64.b64encode(json.dumps(query_json).encode('utf-8')).decode('utf-8')
        reqresult = requests.post(job.valjob.network.slpdb_host + "/q/", json=query_json, timeout=60)
        depends_on = []
        depths = []
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            depends_on.extend(resp['dependsOn'])
            depths.extend(resp['depths'])
        txns = [(d, Transaction(base64.b64decode(tx).hex())) for d, tx in zip(depths, depends_on)]
        job.txn_count_progress += len(txns)
        for tx in txns:
            SlpGraphSearchManager.tx_cache_put(tx[1])
        if txns:
            job.depth_completed = job.depth_map[str((depth_map_index+1)*1000)][0]
        if job.depth_completed < job.total_depth:
            txids = [tx[1].txid_fast() for tx in txns if tx[0] == query_depth]
            depth_map_index += 1
            self.search_query(job, txids, depth_map_index)
        else:
            job.set_success()
            print("[SLP Graph Search] job success")

    def get_query_json(self, txids, max_depth, host, validity_cache=[]):
        print("[SLP Graph Search] " + str(txids))
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "db": ["g"],
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$graphLookup": {
                        "from": "graphs",
                        "startWith": "$graphTxn.txid",
                        "connectFromField": "graphTxn.txid",
                        "connectToField": "graphTxn.outputs.spendTxid",
                        "as": "dependsOn",
                        "maxDepth": max_depth,
                        "depthField": "depth",
                        "restrictSearchWithMatch": { #TODO: add tokenId restriction to this for NFT1 application
                            "graphTxn.txid": {"$nin": txids }} #validity_cache}}  # TODO: add validity_cache here also
                    }},
                    {"$project":{
                        "_id":0,
                        "tokenId": "$tokenDetails.tokenIdHex",
                        "txid": "$graphTxn.txid",
                        "dependsOn": {
                            "$map":{
                                "input": "$dependsOn.graphTxn.txid",
                                "in": "$$this"

                            }
                        },
                        "depths": {
                            "$map":{
                                "input": "$dependsOn.depth",
                                "in": "$$this"
                            }
                        }
                        }
                    },
                    {"$unwind": {
                        "path": "$dependsOn", "includeArrayIndex": "depends_index"
                        }
                    },
                    {"$unwind":{
                        "path": "$depths", "includeArrayIndex": "depth_index"
                        }
                    },
                    {"$project": {
                        "tokenId": 1,
                        "txid": 1,
                        "dependsOn": 1,
                        "depths": 1,
                        "compare": {"$cmp":["$depends_index", "$depth_index"]}
                        }
                    },
                    {"$match": {
                        "compare": 0
                        }
                    },
                    {"$group": {
                        "_id":"$dependsOn",
                        "txid": {"$first": "$txid"},
                        "tokenId": {"$first": "$tokenId"},
                        "depths": {"$push": "$depths"}
                        }
                    },
                    {"$lookup": {
                        "from": "confirmed",
                        "localField": "_id",
                        "foreignField": "tx.h",
                        "as": "tx"
                        }
                    },
                    {"$project": {
                        "txid": 1,
                        "tokenId": 1,
                        "depths": 1,
                        "dependsOn": "$tx.tx.raw",
                        "_id": 0
                        }
                    },
                    {
                        "$unwind": "$dependsOn"
                    },
                    {
                        "$unwind": "$depths"
                    },
                    {
                        "$sort": {"depths": 1}
                    },
                    {
                        "$group": {
                            "_id": "$txid",
                            "dependsOn": {"$push": "$dependsOn"},
                            "depths": {"$push": "$depths"},
                            "tokenId": {"$first": "$tokenId"}
                        }
                    },
                    {
                        "$project": {
                            "txid": "$_id",
                            "tokenId": 1,
                            "dependsOn": 1,
                            "depths": 1,
                            "_id": 0,
                            "txcount": { "$size": "$dependsOn" }
                        }
                    }
                ],
                "limit": len(txids)  # we will get a maximum of len(txids) results in form of the final $projection
            }
            }

        return q

    # This cache stores foreign (non-wallet) tx's we fetched from the network
    # for the purposes of the "fetch_input_data" mechanism. Its max size has
    # been thoughtfully calibrated to provide a decent tradeoff between
    # memory consumption and UX.
    #
    # In even aggressive/pathological cases this cache won't ever exceed
    # 100MB even when full. [see ExpiringCache.size_bytes() to test it].
    # This is acceptable considering this is Python + Qt and it eats memory
    # anyway.. and also this is 2019 ;). Note that all tx's in this cache
    # are in the non-deserialized state (hex encoded bytes only) as a memory
    # savings optimization.  Please maintain that invariant if you modify this
    # code, otherwise the cache may grow to 10x memory consumption if you
    # put deserialized tx's in here.
    _fetched_tx_cache = ExpiringCache(maxlen=100000, name="GraphSearchTxnFetchCache")

    @classmethod
    def tx_cache_get(cls, txid : str) -> object:
        ''' Attempts to retrieve txid from the tx cache that this class
        keeps in-memory.  Returns None on failure. The returned tx is
        not deserialized, and is a copy of the one in the cache. '''
        tx = cls._fetched_tx_cache.get(txid)
        if tx is not None and tx.raw:
            # make sure to return a copy of the transaction from the cache
            # so that if caller does .deserialize(), *his* instance will
            # use up 10x memory consumption, and not the cached instance which
            # should just be an undeserialized raw tx.
            return Transaction(tx.raw)
        return None

    @classmethod
    def tx_cache_put(cls, tx : object, txid : str = None):
        ''' Puts a non-deserialized copy of tx into the tx_cache. '''
        if not tx or not tx.raw:
            raise ValueError('Please pass a tx which has a valid .raw attribute!')
        txid = txid or Transaction._txid(tx.raw)  # optionally, caller can pass-in txid to save CPU time for hashing
        cls._fetched_tx_cache.put(txid, Transaction(tx.raw))
