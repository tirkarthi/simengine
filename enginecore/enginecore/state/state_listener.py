""" StateListener monitors any updates to assets/OIDs """
import sys

from circuits import Component, Event, Timer, Worker
import redis

from enginecore.state.assets import SUPPORTED_ASSETS
from enginecore.state.event_map import event_map
from enginecore.state.graph_reference import GraphReference

class StateListener(Component):
    """ Top-level component that instantiates assets & maps redis events to circuit events"""


    def __init__(self):
        super(StateListener, self).__init__()

        # subscribe to redis key events
        self.redis_store = redis.StrictRedis(host='localhost', port=6379)
        self.pubsub = self.redis_store.pubsub()
        self.pubsub.psubscribe('__key*__:*')

        # assets will store all the devices/items including PDUs, switches etc.
        self._assets = {}
        self._graph_db = GraphReference().get_session()

        # query graph db for the nodes labeled as `Asset`
        results = self._graph_db.run(
            "MATCH (asset:Asset) return asset"
        )

        # instantiate assets based on graph records
        for record in results:
            try:
                asset_label = set(SUPPORTED_ASSETS).intersection(
                    map(lambda x: x.lower(), record['asset'].labels)
                )

                asset_key = record['asset'].get('key')
                asset_label = next(iter(asset_label), '').lower()

                self._assets[asset_key] = SUPPORTED_ASSETS[asset_label](asset_key).register(self)     

            except KeyError:
                print('Detected asset that is not supported', file=sys.stderr)

        Worker(process=False).register(self)


    def monitor(self):
        """ listens to redis events """

        print("...")
        message = self.pubsub.get_message()

        # validate message
        if ((not message) or ('data' not in message) or (not isinstance(message['data'], bytes))):
            return

        data = message['data'].decode("utf-8")
        value = (self.redis_store.get(data)).decode()
        asset_key, property_id = data.split('-')

        try:

            if property_id in SUPPORTED_ASSETS:
                updated_asset = self._assets[int(asset_key)]
                self.fire(event_map[property_id][value], updated_asset)

                # look up child nodes
                results = self._graph_db.run(
                    "MATCH (asset:Asset)-[:POWERED_BY]->({ key: $key }) return asset",
                    key=int(asset_key)
                )

                for record in results:
                    key = record['asset'].get('key')
                    self.fire(event_map[property_id][value], self._assets[key])

                print("Key: {}-{} -> {}".format(asset_key, property_id.replace(" ", ""), value))
            elif int(asset_key) in self._assets:
                oid = property_id.replace(" ", "")
                _, oid_value = value.split("|")

                # look up dependant nodes
                results = self._graph_db.run(
                    "MATCH (asset:Asset)-[:POWERED_BY]->(oid:OID { OID: $oid }) return asset, oid",
                    oid=oid
                )

                for record in results:
                    key = record['asset'].get('key')
                    oid_name = record['oid']['OIDName']

                    self.fire(event_map[oid_name][oid_value], self._assets[key])

                print('oid changed:')
                print(">" + oid + ": " + oid_value)

        except KeyError as error:
            print("Detected unregistered asset under key [{}]".format(error), file=sys.stderr)


    def get_assets(self):
        """ running instances """
        return self._assets


    def started(self, *args):
        """
            Called on start
        """
        print('Listening to Redis events')
        # check redis state every second
        Timer(1, Event.create("monitor"), persist=True).register(self)

    def __exit__(self, exc_type, exc_value, traceback):
        self._graph_db.close()

if __name__ == '__main__':
    StateListener().run()
