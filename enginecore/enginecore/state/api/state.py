
import time
import os
import tempfile
import subprocess
import json
from websocket import create_connection

import redis

from enginecore.model.graph_reference import GraphReference
from enginecore.state.utils import format_as_redis_key
from enginecore.state.redis_channels import RedisChannels

from enginecore.state.asset_definition import SUPPORTED_ASSETS
from enginecore.state.recorder import RECORDER as record


class IStateManager():
    """Base class for all the state managers """

    redis_store = None

    def __init__(self, asset_info):
        self._graph_ref = GraphReference()
        self._asset_key = asset_info['key']
        self._asset_info = asset_info


    @property
    def key(self):
        """Asset Key """
        return self._asset_key


    @property
    def redis_key(self):
        """Asset key in redis format as '{key}-{type}' """
        return "{}-{}".format(str(self.key), self.asset_type)


    @property
    def asset_type(self):
        """Asset Type """
        return self._asset_info['type']


    @property
    def power_usage(self):
        """Normal power usage in AMPS when powered up"""
        return 0


    @property
    def draw_percentage(self):
        """How much power the asset draws"""
        return self._asset_info['draw'] if 'draw' in self._asset_info else 1


    @property
    def load(self):
        """Get current load stored in redis (in AMPs)"""
        return float(IStateManager.get_store().get(self.redis_key + ":load"))


    @property
    def wattage(self):
        """Asset wattage (assumes power-source to be 120v)"""
        return self.load * 120


    @property
    def status(self):
        """Operational State 
        
        Returns:
            int: 1 if on, 0 if off
        """
        return int(IStateManager.get_store().get(self.redis_key))


    @property
    def agent(self):
        """Agent instance details (if supported)
        
        Returns:
            tuple: process id and status of the process (if it's running)
        """
        pid = IStateManager.get_store().get(self.redis_key + ":agent")
        return (int(pid), os.path.exists("/proc/" + pid.decode("utf-8"))) if pid else None


    @record
    def shut_down(self):
        """Implements state logic for graceful power-off event, sleeps for the pre-configured time
            
        Returns:
            int: Asset's status after power-off operation
        """
        print('SHUTTING DOWN {}'.format(self.key))
        self._sleep_shutdown()
        if self.status:
            self._set_state_off()
        return self.status


    @record
    def power_off(self):
        """Implements state logic for abrupt power loss 
        
        Returns:
            int: Asset's status after power-off operation
        """
        print('POWERING OFF {}'.format(self.key))

        if self.status:
            self._set_state_off()
        return self.status


    @record
    def power_up(self):
        """Implements state logic for power up, sleeps for the pre-configured time & resets boot time
        
        Returns:
            int: Asset's status after power-on operation
        """
        print('POWERING UP {}'.format(self.key))

        if self._parents_available() and not self.status:
            self._sleep_powerup()
            # udpate machine start time & turn on
            self._reset_boot_time()
            self._set_state_on()
        return self.status


    def _update_load(self, load):
        """Update amps"""
        load = load if load >= 0 else 0
        IStateManager.get_store().set(self.redis_key + ":load", load)
        self._publish_load()


    def _publish_load(self):
        """Publish load changes """
        IStateManager.get_store().publish(RedisChannels.load_update_channel, self.redis_key)


    def _sleep_delay(self, delay_type):
        """Sleep for n number of ms determined by the delay_type"""
        if delay_type in self._asset_info:
            time.sleep(self._asset_info[delay_type] / 1000.0) # ms to sec


    def _sleep_shutdown(self):
        """Hardware-specific shutdown delay"""
        self._sleep_delay('offDelay')


    def _sleep_powerup(self):
        """Hardware-specific powerup delay"""
        self._sleep_delay('onDelay')
    

    def _set_redis_asset_state(self, state, publish=True):
        """Update redis value of the asset power status"""
        IStateManager.get_store().set(self.redis_key, state)
        if publish:
            self._publish_power()


    def _set_state_on(self):
        """Set state to online"""
        self._set_redis_asset_state('1')


    def _set_state_off(self):
        """Set state to offline"""
        self._set_redis_asset_state('0')


    def _publish_power(self):
        """Notify daemon of power updates"""
        IStateManager.get_store().publish(
            RedisChannels.state_update_channel, "{}-{}".format(self.redis_key, self.status)
        )


    def _get_oid_value(self, oid, key):
        """Retrieve value for a specific OID """
        redis_store = IStateManager.get_store() 
        rkey = format_as_redis_key(str(key), oid, key_formatted=False)
        return redis_store.get(rkey).decode().split('|')[1]


    def _reset_boot_time(self):
        """Reset device start time (used to calculate uptime)"""
        IStateManager.get_store().set(str(self._asset_key) + ":start_time", int(time.time())) 


    def _update_oid_by_name(self, oid_name, oid_type, value, use_spec=False):
        """Update a specific oid
        Args:
            oid_name(str): oid name defined in device preset file
            oid_type(rfc1902): oid type (rfc1902 specs)
            value(str): oid value or spec parameter if use_spec is set to True
            use_spec:
        Returns:
            bool: true if oid was successfully updated
        """

        with self._graph_ref.get_session() as db_s:
            oid, data_type, oid_spec = GraphReference.get_asset_oid_by_name(
                db_s, int(self._asset_key), oid_name
            )

            if oid:
                new_oid_value = oid_spec[value] if use_spec and oid_spec else value
                self._update_oid_value(oid, data_type, oid_type(new_oid_value))
                return True
            
            return False


    def _update_oid_value(self, oid, data_type, oid_value):
        """Update oid with a new value
        
        Args:
            oid(str): SNMP object id
            data_type(int): Data type in redis format
            oid_value(object): OID value in rfc1902 format 
        """
        redis_store = IStateManager.get_store() 
        
        rvalue = "{}|{}".format(data_type, oid_value)
        rkey = format_as_redis_key(str(self._asset_key), oid, key_formatted=False)

        redis_store.set(rkey, rvalue)


    def _check_parents(self, keys, parent_down, msg='Cannot perform the action: [{}] parent is off'):
        """Check that redis values pass certain condition
        
        Args:
            keys (list): Redis keys (formatted as required)
            parent_down (callable): lambda clause 
            msg (str, optional): Error message to be printed
        
        Returns: 
            bool: True if parent keys are missing or all parents were verified with parent_down clause 
        """
        if not keys:
            return True

        parent_values = IStateManager.get_store().mget(keys)
        pdown = 0
        pdown_msg = ''
        for rkey, rvalue in zip(keys, parent_values): 
            if parent_down(rvalue, rkey):
                pdown_msg += msg.format(rkey) + '\n'
                pdown += 1       
         
        if pdown == len(keys):
            print(pdown_msg)
            return False

        return True


    def _parents_available(self):
        """Indicates whether a state action can be performed;
        checks if parent nodes are up & running and all OIDs indicate 'on' status
        
        Returns:
            bool: True if parents are available
        """
        
        not_affected_by_mains = True
        asset_keys, oid_keys = GraphReference.get_parent_keys(self._graph_ref.get_session(), self._asset_key)

        if not asset_keys and not IStateManager.mains_status():
            not_affected_by_mains = False

        assets_up = self._check_parents(asset_keys, lambda rvalue, _: rvalue == b'0')
        oid_clause = lambda rvalue, rkey: rvalue.split(b'|')[1].decode() == oid_keys[rkey]['switchOff']
        oids_on = self._check_parents(oid_keys.keys(), oid_clause)

        return assets_up and oids_on and not_affected_by_mains
    

    @classmethod
    def get_temp_workplace_dir(cls):
        """Get location of the temp directory"""
        sys_temp = tempfile.gettempdir()
        simengine_temp = os.path.join(sys_temp, 'simengine')
        return simengine_temp
    

    @classmethod 
    def get_store(cls):
        """Get redis db handler """
        if not cls.redis_store:
            cls.redis_store = redis.StrictRedis(host='localhost', port=6379)

        return cls.redis_store


    @classmethod 
    def _get_assets_states(cls, assets, flatten=True): 
        """Query redis store and find states for each asset
        
        Args:
            flatten(bool): If false, the returned assets in the dict will have their child-components nested
        Returns:
            dict: Current information on assets including their states, load etc.
        """
        asset_keys = assets.keys()
        
        if not asset_keys:
            return None

        asset_values = cls.get_store().mget(
            list(map(lambda k: "{}-{}".format(k, assets[k]['type']), asset_keys))
        )

        for rkey, rvalue in zip(assets, asset_values):

            asset_state = cls.get_state_manager_by_key(rkey, SUPPORTED_ASSETS)

            assets[rkey]['status'] = int(rvalue)
            assets[rkey]['load'] = asset_state.load
            
            if assets[rkey]['type'] == 'ups':
                assets[rkey]['battery'] = asset_state.battery_level

            if not flatten and 'children' in assets[rkey]:
                # call recursively on children    
                assets[rkey]['children'] = cls._get_assets_states(assets[rkey]['children'])

        return assets


    @classmethod
    def get_system_status(cls, flatten=True):
        """Get states of all system components 
        
        Args:
            flatten(bool): If false, the returned assets in the dict will have their child-components nested
        
        Returns:
            dict: Current information on assets including their states, load etc.
        """
        graph_ref = GraphReference()
        with graph_ref.get_session() as session:

            # cache assets 
            assets = GraphReference.get_assets_and_connections(session, flatten)
            assets = cls._get_assets_states(assets, flatten)
            return assets


    @classmethod
    def reload_model(cls):
        """Request daemon reloading"""
        cls.get_store().publish(RedisChannels.model_update_channel, 'reload')


    @classmethod
    @record
    def power_outage(cls):
        """Simulate complete power outage/restoration"""
        cls.get_store().set('mains-source', '0')
        cls.get_store().publish(RedisChannels.mains_update_channel, '0')


    @classmethod
    @record
    def power_restore(cls):
        """Simulate complete power restoration"""
        cls.get_store().set('mains-source', '1')
        cls.get_store().publish(RedisChannels.mains_update_channel, '1')
    

    @classmethod
    def mains_status(cls):
        """Get wall power status"""
        return int(cls.get_store().get('mains-source').decode())


    @classmethod
    def get_ambient(cls):
        """Retrieve current ambient value"""
        temp = cls.get_store().get('ambient')
        return int(temp.decode()) if temp else 0


    @classmethod
    def set_ambient(cls, value):
        """Update ambient value"""
        old_temp = cls.get_ambient()
        cls.get_store().set('ambient', str(int(value)))
        cls.get_store().publish(RedisChannels.ambient_update_channel, '{}-{}'.format(old_temp, value))  
    
    
    @classmethod
    def get_ambient_props(cls):
        """Get runtime ambient properties (ambient behaviour description)"""
        graph_ref = GraphReference()
        with graph_ref.get_session() as session:
            props = GraphReference.get_ambient_props(session)
            return props


    @classmethod
    def set_ambient_props(cls, props):
        """Update runtime thermal properties of the room temperature"""

        graph_ref = GraphReference()
        with graph_ref.get_session() as session: 
            GraphReference.set_ambient_props(session, props)


    @classmethod
    def set_play_path(cls, path):
        """Update play folder containing scripts"""

        graph_ref = GraphReference()
        with graph_ref.get_session() as session: 
            GraphReference.set_play_path(session, path)


    @classmethod
    def plays(cls):
        """Get plays available for execution
        Returns:
            tuple: list of bash files as well as python scripts
        """

        graph_ref = GraphReference()
        with graph_ref.get_session() as session:

            play_path = GraphReference.get_play_path(session)
            if not play_path:
                return ([], [])

            play_files = [f for f in os.listdir(play_path) if os.path.isfile(os.path.join(play_path, f))]

            return(
                [os.path.splitext(f)[0] for f in play_files if os.path.splitext(f)[1] != '.py'],
                [os.path.splitext(f)[0] for f in play_files if os.path.splitext(f)[1] == '.py']
            )


    @classmethod
    def execute_play(cls, play_name):
        """Execute a specific play
        Args:
            play_name(str): playbook name
        """

        graph_ref = GraphReference()
        with graph_ref.get_session() as session:

            play_path = GraphReference.get_play_path(session)
            if not play_path:
                return

            file_filter = lambda f: os.path.isfile(os.path.join(play_path, f)) and os.path.splitext(f)[0] == play_name

            play_file = [
                f for f in os.listdir(play_path) if file_filter(f)
            ][0]

            subprocess.Popen(
                os.path.join(play_path, play_file), stderr=subprocess.DEVNULL, close_fds=True
            )


    @classmethod
    def get_state_manager_by_key(cls, key, supported_assets):
        """Infer asset manager from key"""

        graph_ref = GraphReference()
        
        with graph_ref.get_session() as session:

            asset_info = GraphReference.get_asset_and_components(session, key)
            sm_mro = supported_assets[asset_info['type']].StateManagerCls.mro()
            
            module = 'enginecore.state.api'
            return next(filter(lambda x: x.__module__.startswith(module), sm_mro))(asset_info)


class StateClient():
    """A web-socket client responsible for state"""

    socket_conf = {
        'host': os.environ.get('SIMENGINE_SOCKET_HOST', "0.0.0.0"),
        'port': os.environ.get('SIMENGINE_SOCKET_PORT', int(8000))
    }


    def __init__(self, key):
        self._asset_key = key
        self._ws_client = StateClient.get_ws_client()


    @classmethod
    def get_ws_client(cls):
        return create_connection("ws://{host}:{port}/simengine".format(**StateClient.socket_conf))


    @classmethod
    def send_request(cls, request, data, ws_client=None):
        if not ws_client:
            ws_client = StateClient.get_ws_client()

        ws_client.send(json.dumps({"request": request, "payload": data}))


    def power_up(self):
        StateClient.send_request("power", {"status": 1, "key": self._asset_key}, self._ws_client)


    def shut_down(self):
        StateClient.send_request("power", {"status": 0, "key": self._asset_key}, self._ws_client)


    @classmethod
    def power_outage(cls):
        """Simulate complete power outage/restoration"""
        StateClient.send_request("mains", {"mains": 0})


    @classmethod
    def power_restore(cls):
        """Simulate complete power restoration"""
        StateClient.send_request("mains", {"mains": 1})


    @classmethod
    def replay_all(cls, slc=slice(None, None)):
        StateClient.send_request(
            "replay-actions",
            {
                "range": {"start": slc.start, "end": slc.end, "step": slc.step}
            }
        )


    @classmethod
    def clear_actions(cls, slc=slice(None, None)):
        StateClient.send_request(
            "purge-actions",
            {
                "range": {"start": slc.start, "end": slc.end, "step": slc.step}
            }
        )
