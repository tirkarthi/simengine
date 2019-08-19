"""Engine monitors any updates to assets/OIDs 
& determines if the event affects other (connected) assets

The daemon initializes a WebSocket, handles redis channel update
and reacts to state updates by dispatching circuits events that are, in turn,
are processed by individual assets.
"""
import logging
import os
import math
import functools

from circuits import Component, Event, Worker, Debugger, handler  # , task
import redis

from circuits.web import Logger, Server, Static
from circuits.web.dispatchers import WebSocketsDispatcher

from enginecore.state.hardware.event_results import (
    PowerEventResult,
    LoadEventResult,
    VoltageEventResult,
)
from enginecore.state.hardware.room import ServerRoom, Asset

from enginecore.tools.recorder import RECORDER
from enginecore.state.api import ISystemEnvironment
from enginecore.state.event_map import PowerEventMap
from enginecore.state.net.ws_server import WebSocket
from enginecore.state.net.ws_requests import ServerToClientRequests

from enginecore.state.redis_channels import RedisChannels
from enginecore.model.graph_reference import GraphReference
from enginecore.state.state_initializer import initialize, clear_temp


class NotifyClient(Event):
    """Notify websocket clients of any data updates"""


class Engine(Component):
    """Top-level component that instantiates assets 
    & maps redis events to circuit events"""

    def __init__(self, debug=False, force_snmp_init=True):
        super(Engine, self).__init__()

        ### Set-up WebSocket & Redis listener ###
        logging.info("Starting simengine daemon...")

        # Use redis pub/sub communication
        logging.info("Initializing redis connection...")
        self._redis_store = redis.StrictRedis(host="localhost", port=6379)

        # assets will store all the devices/items including PDUs, switches etc.
        self._assets = {}
        self._sys_environ = ServerRoom().register(self)

        # init graph db instance
        logging.info("Initializing neo4j connection...")
        self._graph_ref = GraphReference()

        # set up a web socket server
        socket_conf = {
            "host": os.environ.get("SIMENGINE_SOCKET_HOST"),
            "port": int(os.environ.get("SIMENGINE_SOCKET_PORT")),
        }

        logging.info(
            "Initializing websocket server at %s:%s ...",
            socket_conf["host"],
            socket_conf["port"],
        )
        self._server = Server((socket_conf["host"], socket_conf["port"])).register(self)

        Worker(process=False).register(self)
        Static().register(self._server)
        Logger().register(self._server)

        if debug:
            Debugger(events=False).register(self)

        self._ws = WebSocket().register(self._server)
        WebSocketsDispatcher("/simengine").register(self._server)

        # Register assets and reset power state
        self._reload_model(force_snmp_init)

        logging.info("Physical Environment:\n%s", self._sys_environ)

    def _reload_model(self, force_snmp_init=True):
        """Re-create system topology (instantiate assets based on graph ref)"""

        RECORDER.enabled = False
        logging.info("Initializing system topology...")

        self._assets = {}

        # init state
        clear_temp()
        initialize(force_snmp_init)

        # get system topology
        with self._graph_ref.get_session() as session:
            assets = GraphReference.get_assets_and_children(session)

        for asset in assets:
            self._assets[asset["key"]] = Asset.get_supported_assets()[asset["type"]](
                asset
            ).register(self)

        ISystemEnvironment.set_ambient(21)
        RECORDER.enabled = True

    def _handle_oid_update(self, asset_key, oid, value):
        """React to OID update in redis store 
        Args:
            asset_key(int): key of the asset oid belongs to
            oid(str): updated oid
            value(str): OID value in snmpsim format "datatype|value"
        """
        if asset_key not in self._assets:
            logging.warning("Asset [%s] does not exist!", asset_key)
            return

        oid = oid.replace(" ", "")
        _, oid_value = value.split("|")
        with self._graph_ref.get_session() as session:
            affected_keys, oid_details = GraphReference.get_asset_oid_info(
                session, asset_key, oid
            )

        if not oid_details:
            logging.warning(
                "OID:[%s] for asset:[%s] cannot be processed by engine!", oid, asset_key
            )
            return

        oid_value_name = oid_details["specs"][oid_value]
        oid_name = oid_details["name"]

        for key in affected_keys:
            self.fire(
                PowerEventMap.get_state_specs()[oid_name][oid_value_name],
                self._assets[key],
            )

        logging.info("oid changed:")
        logging.info(">" + oid + ": " + oid_value)

    def _handle_ambient_update(self, new_temp, old_temp):
        """React to ambient update by notifying all the assets in the sys topology
        Args:
            new_temp(float): new ambient value
            old_temp(float): old ambient value
        """

        self._notify_client(
            ServerToClientRequests.ambient_upd,
            {"ambient": new_temp, "rising": new_temp > old_temp},
        )
        for a_key in self._assets:
            self.fire(
                PowerEventMap.map_ambient_event(old_temp, new_temp), self._assets[a_key]
            )

    def _handle_voltage_update(self, old_voltage, new_voltage):
        """let devices handle voltage updates"""

        with self._graph_ref.get_session() as session:
            mains_out_keys = GraphReference.get_mains_powered_outlets(session)

        mains_out = {k: self._assets[k] for k in mains_out_keys if k}

        for outlet in mains_out.values():
            self.fire(PowerEventMap.map_voltage_event(old_voltage, new_voltage), outlet)

    def _handle_state_update(self, asset_key, asset_status):
        """React to asset state updates in redis store 
        Args:
            asset_key(int): key of the updated asset
            asset_status(str): updated status of the asset under asset key
        """

        updated_asset = self._assets[asset_key]

        # write to a web socket
        self._notify_client(
            ServerToClientRequests.asset_upd, {"key": asset_key, "status": asset_status}
        )

        # power button pressed event:
        self.fire(PowerEventMap.map_asset_event(asset_status), updated_asset)

        # fire-up power events down the power stream
        self._chain_power_update(
            PowerEventResult(asset_key=asset_key, new_state=asset_status)
        )

    def _chain_power_update(self, event_result: PowerEventResult):
        """React to power state event by analysing the parent,
        child & neighbouring assets
        
        Args:
            event_result: contains data about power state update 
                          event such as key of the affected asset, 
                          its old state & new state
        Example:
            when a node is powered down, 
            the assets it powers should be powered down as well
        """

        updated_asset = self._assets[event_result.asset_key]
        new_state = event_result.new_state

        with self._graph_ref.get_session() as session:
            children, parent_info, _2nd_parent = GraphReference.get_affected_assets(
                session, updated_asset.key
            )

        alt_parent_asset = self._assets[_2nd_parent["key"]] if _2nd_parent else None
        parent_assets = list(map(lambda p: self._assets[p["key"]], parent_info))

        # Meaning it's a leaf node -> update load up the power chain if needed
        if not children and parent_assets:

            volt_change = (1 if new_state else -1) * updated_asset.state.power_usage
            self._process_leaf_node_power_event(
                updated_asset, volt_change, parent_assets
            )

        # Check assets down the power stream (assets powered by the updated asset)
        for child in children:
            self._process_int_node_power_event(
                updated_asset, self._assets[child["key"]], new_state, alt_parent_asset
            )

    def _get_affected_assets(self, updated_asset):
        """Get neighbouring hardware devices of the updated asset"""

        with self._graph_ref.get_session() as session:
            children, parent_info, _2nd_parent = GraphReference.get_affected_assets(
                session, updated_asset.key
            )

        child_assets = list(map(lambda a: self._assets[a["key"]], children))
        parent_assets = list(map(lambda a: self._assets[a["key"]], parent_info))
        alt_parent_asset = self._assets[_2nd_parent["key"]] if _2nd_parent else None

        return child_assets, parent_assets, alt_parent_asset

    def _chain_voltage_update(
        self, volt_e_result: VoltageEventResult, power_e_result: PowerEventResult = None
    ):
        """Chain voltage updates down the power stream
        """

        old_volt, new_volt = volt_e_result.old_voltage, volt_e_result.new_voltage
        updated_asset = self._assets[volt_e_result.asset_key]

        if old_volt == new_volt:
            return

        child_assets, parent_assets, _ = self._get_affected_assets(updated_asset)

        # Output voltage can still be 0 for some assets even though
        # their input voltage > 0
        # (most devices require at least 90V-100V in order to function)
        old_out_volt = old_volt * (power_e_result.old_state if power_e_result else 1)
        new_out_volt = new_volt * (power_e_result.new_state if power_e_result else 1)

        volt_event = functools.partial(
            PowerEventMap.map_voltage_event, old_volt, new_out_volt
        )

        # internal node: fire voltage updates down the power stream
        # (children powered by the updated asset)
        for child in child_assets:
            self.fire(volt_event(), child)

        # find load difference between old & new state
        upd_asset_load = lambda v: updated_asset.state.power_consumption / v if v else 0
        load_change = upd_asset_load(new_out_volt) - upd_asset_load(old_out_volt)

        # process leaf node, update load up the power chain
        if parent_assets and load_change:
            self._process_leaf_node_power_event(
                updated_asset, load_change, parent_assets
            )

    def _chain_load_update(self, event_result: LoadEventResult):
        """React to load update event by propogating the load changes 
        up the power stream
        
        Args:
            event_result: contains data about load update event such as key of 
                          the affected asset, its old load, new load and load change 

        Example:
            when a leaf node is powered down, its load is set to 0 
            & parent assets get updated load values
        """
        load_change = abs(event_result.old_load - event_result.new_load)

        child_asset = self._assets[event_result.asset_key]
        child_event = (
            PowerEventMap.map_load_increased_by
            if event_result.old_load < event_result.new_load
            else PowerEventMap.map_load_decreased_by
        )

        with self._graph_ref.get_session() as session:
            parent_assets = GraphReference.get_parent_assets(session, child_asset.key)

        for parent_info in parent_assets:
            parent = self._assets[parent_info["key"]]

            # load was already updated for ups parent
            if child_asset.state.asset_type == "ups" and not parent.state.status:
                return

            # notify parent node of child asset load update
            parent_load_change = load_change * parent.state.draw_percentage
            self.fire(child_event(parent_load_change, child_asset.key), parent)

    def _process_leaf_node_power_event(
        self, updated_asset: Asset, load_change: float, parent_assets: list
    ):
        """React to leaf node power event (power up/down) by firing load updates up
        the power supply chain.
        Args:
            updated_asset: leaf node asset with new power state
            load_change: load change
            parent_assets: Assets powering the leaf node
        """
        offline_parents_load = 0
        online_parents = []

        for parent in parent_assets:

            parent_load_change = load_change * parent.state.draw_percentage

            if not parent.state.load and not parent.state.status:
                # if offline -> find how much power parent should draw
                # (so it will be redistributed among other assets)
                offline_parents_load += parent_load_change
            else:
                online_parents.append(parent)

        # for each parent that is either online or it's load is not zero
        # update the load value
        for parent in online_parents:

            leaf_node_amp = load_change * parent.state.draw_percentage

            load_upd = offline_parents_load + leaf_node_amp
            # fire load increase/decrease depending on the
            # new state of the updated asset
            if load_change > 0:
                p_event = PowerEventMap.map_load_increased_by
            else:
                p_event = PowerEventMap.map_load_decreased_by

            self.fire(p_event(abs(load_upd), updated_asset.key), parent)

        # if new_state == 0:
        #     updated_asset.state.update_load(0)

    def _process_int_node_power_event(
        self,
        updated_asset: Asset,
        child_asset: Asset,
        new_state: int,
        alt_parent_asset: Asset = None,
    ):
        r"""React to internal node's (a node with at least one child) power event
        by updating power state of its child (if needed).

        Args:
            updated_asset: internal node whose power state has been changed
            child_asset: child node of the updated asset
            new_state: new state of the updated asset
            alt_parent_asset: second (or altertnative) parent of the child asset

        Example:
            if (psu1) went down, its child (server) will be powered off depending
            on whether alternative parent (psu2) is functioning or not

            (psu1)  (psu2)
              \       /
             [pow]  [pow]
                \   /
                (server) <- child
        """

        child_load = child_asset.state.power_usage * updated_asset.state.draw_percentage

        get_child_load_event = lambda new_state: (
            PowerEventMap.map_load_decreased_by,
            PowerEventMap.map_load_increased_by,
        )[new_state](child_load, child_asset.key)

        # power up/down child assets if there's no alternative power source
        if not (alt_parent_asset and alt_parent_asset.state.status):

            out_volt = updated_asset.state.output_voltage
            event = PowerEventMap.map_voltage_event(
                new_value=new_state * out_volt, old_value=(new_state ^ 1) * out_volt
            )

            self.fire(event, child_asset)

            # Special case for UPS
            # ups won't be powered off but the load has to change anyways
            if (
                child_asset.state.asset_type == "ups"
                and child_asset.state.battery_level
            ):
                self.fire(get_child_load_event(new_state), updated_asset)

        # check upstream & branching power
        # alternative power source is available,
        # therefore the load needs to be re-distributed
        else:
            logging.info(
                "Asset[%s] has alternative parent[%s]",
                child_asset.key,
                alt_parent_asset.key,
            )

            # increase/decrease power on the neighbouring power stream
            # (how much updated asset was drawing)
            self.fire(get_child_load_event(new_state ^ 1), alt_parent_asset)

            # change load up the node power stream (power source of the updated node)
            load_child_event = PowerEventMap.map_child_event(
                new_state, child_load, updated_asset.key
            )
            self.fire(load_child_event, updated_asset)

    def _notify_client(self, client_request, data):
        """Notify the WebSocket client(s) of any changes in asset states 

        Args:
            client_request(ServerToClientRequests): type of data passed to the ws client
            data(dict): updated key/values (e.g. status, load)
        """

        self.fire(
            NotifyClient({"request": client_request.name, "payload": data}), self._ws
        )

    # -- Handle Power Changes --
    @handler(RedisChannels.state_update_channel)
    def on_asset_power_state_change(self, data):
        """On user changing asset status"""
        self._handle_state_update(data["key"], data["status"])

    @handler(RedisChannels.voltage_update_channel)
    def on_voltage_state_change(self, data):
        """React to voltage drop or voltage restoration"""
        self._handle_voltage_update(data["old_voltage"], data["new_voltage"])

    @handler(RedisChannels.mains_update_channel)
    def on_wallpower_state_change(self, data):
        """On balckouts/power restorations"""
        self._notify_client(ServerToClientRequests.mains_upd, {"mains": data["status"]})
        self.fire(PowerEventMap.map_mains_event(data["status"]), self._sys_environ)

    @handler(RedisChannels.oid_update_channel)
    def on_snmp_device_oid_change(self, data):
        """React to OID getting updated through SNMP interface"""
        value = (self._redis_store.get(data)).decode()
        asset_key, oid = data.split("-")
        self._handle_oid_update(int(asset_key), oid, value)

    @handler(RedisChannels.model_update_channel)
    def on_model_reload_reqeust(self, _):
        """Detect topology changes to the system architecture"""
        self._reload_model()

    # -- Battery Updates --
    @handler(RedisChannels.battery_update_channel)
    def on_battery_level_change(self, data):
        """On UPS battery charge drop/increase"""
        self._notify_client(
            ServerToClientRequests.asset_upd,
            {
                "key": data["key"],
                "battery": self._assets[data["key"]].state.battery_level,
            },
        )

    @handler(RedisChannels.battery_conf_charge_channel)
    def on_battery_charge_factor_up(self, data):
        """On UPS battery charge increase"""
        self._assets[data["key"]].charge_speed_factor = data["factor"]

    @handler(RedisChannels.battery_conf_drain_channel)
    def on_battery_charge_factor_down(self, data):
        """On UPS battery charge increase"""
        self._assets[data["key"]].drain_speed_factor = data["factor"]

    # -- Thermal Updates --
    @handler(RedisChannels.ambient_update_channel)
    def on_ambient_temperature_change(self, data):
        """Ambient updated"""
        self._handle_ambient_update(data["new_ambient"], data["old_ambient"])

    @handler(RedisChannels.sensor_conf_th_channel)
    def on_new_sensor_thermal_impact(self, data):
        """Add new thermal impact (sensor to sensor)"""
        self._assets[data["key"]].add_sensor_thermal_impact(**data["relationship"])

    @handler(RedisChannels.cpu_usg_conf_th_channel)
    def on_new_cpu_thermal_impact(self, data):
        """Add new thermal impact (cpu load to sensor)"""
        self._assets[data["key"]].add_cpu_thermal_impact(**data["relationship"])

    @handler(RedisChannels.str_cv_conf_th_channel)
    def on_new_cv_thermal_impact(self, data):
        """Add new thermal impact (sensor to cv)"""
        self._assets[data["key"]].add_storage_cv_thermal_impact(**data["relationship"])

    @handler(RedisChannels.str_drive_conf_th_channel)
    def on_new_hd_thermal_impact(self, data):
        """Add new thermal impact (sensor to physical drive)"""
        self._assets[data["key"]].add_storage_pd_thermal_impact(**data["relationship"])

    # **Events are camel-case
    # pylint: disable=C0103,W0613

    ############### Load Events - Callbacks (called only on success)

    def _load_success(self, event_result, increased=True):
        """Handle load event changes by dispatching 
        load update events up the power stream
        """
        self._chain_load_update(event_result)
        if not math.isclose(abs(event_result.new_load - event_result.old_load), 0):
            ckey = int(event_result.asset_key)
            self._notify_client(
                ServerToClientRequests.asset_upd,
                {"key": ckey, "load": self._assets[ckey].state.load},
            )

    # Notify parent asset of any child events
    def ChildAssetPowerDown_success(self, evt, event_result):
        """When child is powered down -> get the new load value of child asset"""

        if self._assets[event_result.asset_key].state.power_consumption != 0:
            self._load_success(event_result)

    def ChildAssetPowerUp_success(self, evt, event_result):
        """When child is powered up -> get the new load value of child asset"""

        if self._assets[event_result.asset_key].state.power_consumption != 0:
            self._load_success(event_result)

    def ChildAssetLoadDecreased_success(self, evt, event_result):
        """When load decreases down the power stream """
        self._load_success(event_result)

    def ChildAssetLoadIncreased_success(self, evt, event_result):
        """When load increases down the power stream """
        self._load_success(event_result)

    ############### Power Events - Callbacks

    def _power_success(self, event_result):
        """Handle power event success by dispatching  
        power events down the power stream"""
        self._notify_client(
            ServerToClientRequests.asset_upd,
            {"key": event_result.asset_key, "status": int(event_result.new_state)},
        )
        self._chain_power_update(event_result)

    def _voltage_success(self, event_results):
        """Process voltage event results"""

        volt_e_result, power_e_result = event_results
        print(volt_e_result, power_e_result)

        if power_e_result and power_e_result.new_state != power_e_result.old_state:
            self._notify_client(
                ServerToClientRequests.asset_upd,
                {
                    "key": power_e_result.asset_key,
                    "status": int(power_e_result.new_state),
                },
            )

        self._chain_voltage_update(volt_e_result, power_e_result)

    def SignalDown_success(self, evt, event_result):
        """When asset is powered down """
        self._power_success(event_result)

    def SignalUp_success(self, evt, event_result):
        """When asset is powered up """
        self._power_success(event_result)

    def SignalReboot_success(self, evt, e_result):
        """Rebooted """

        # need to do power chain
        if not e_result.old_state and e_result.old_state != e_result.new_state:
            self._chain_power_update(e_result)

        self._notify_client(
            ServerToClientRequests.asset_upd,
            {"key": e_result.asset_key, "status": e_result.new_state},
        )

    def VoltageDecreased_success(self, evt, event_results):
        """When asset finished processing new voltage
        and it stayed online"""
        self._voltage_success(event_results)

    def VoltageIncreased_success(self, evt, event_results):
        """When asset finished processing new voltage"""
        self._voltage_success(event_results)
