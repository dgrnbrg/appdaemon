#!/usr/bin/python3
import sys
import importlib
import traceback
import os
import os.path
from queue import Queue
import time
import datetime
import uuid
import astral
import pytz
import math
import asyncio
import yaml
import concurrent
import threading
import random
import re

import appdaemon.utils as utils


class AppDaemon:
    def __init__(self, logger, error, loop, **kwargs):

        self.logger = logger
        self.error = error
        self.config = kwargs["config"]
        self.q = Queue(maxsize=0)

        self.was_dst = False

        self.last_state = None
        self.inits = {}
        # ws = None

        self.monitored_files = {}
        self.modules = {}
        self.appq = None
        self.executor = None
        self.loop = None
        self.srv = None
        self.appd = None
        self.stopping = False

        # Will require object based locking if implemented
        self.objects = {}

        self.schedule = {}
        self.schedule_lock = threading.RLock()

        self.callbacks = {}
        self.callbacks_lock = threading.RLock()

        self.state = {}
        self.state_lock = threading.RLock()

        self.endpoints = {}
        self.endpoints_lock = threading.RLock()

        self.plugins = {}

        # No locking yet
        self.global_vars = {}

        self.sun = {}

        self.config_file_modified = 0
        self.tz = None
        self.ad_time_zone = None
        self.now = 0
        self.realtime = True
        self.version = 0
        self.app_config_file_modified = 0
        self.app_config = None

        self.app_config_file = None
        self._process_arg("app_config_file", kwargs)

        self.plugin_params = kwargs["plugins"]

        # User Supplied/Defaults
        self.threads = 0
        self._process_arg("threads", kwargs)

        self.app_dir = None
        self._process_arg("app_dir", kwargs)

        self.apps = False
        self._process_arg("apps", kwargs)

        self.start_time = None
        self._process_arg("start_time", kwargs)

        self.now = None
        self._process_arg("now", kwargs)

        self.logfile = None
        self._process_arg("logfile", kwargs)

        self.latitude = None
        self._process_arg("latitude", kwargs)

        self.longitude = None
        self._process_arg("longitude", kwargs)

        self.elevation = None
        self._process_arg("elevation", kwargs)

        self.time_zone = None
        self._process_arg("time_zone", kwargs)

        self.errorfile = None
        self._process_arg("error_file", kwargs)

        self.config_file = None
        self._process_arg("config_file", kwargs)

        self.location = None
        self._process_arg("location", kwargs)

        self.tick = 1
        self._process_arg("tick", kwargs)

        self.endtime = None
        self._process_arg("endtime", kwargs)

        self.interval = 1
        self._process_arg("interval", kwargs)

        self.loglevel = "INFO"
        self._process_arg("loglevel", kwargs)

        self.config_dir = None
        self._process_arg("config_dir", kwargs)

        self.api_port = None
        self._process_arg("api_port", kwargs)

        self.utility_delay = 1
        self._process_arg("utility_delay", kwargs)

        #
        # Initial Setup
        #

        self.appq = asyncio.Queue(maxsize=0)

        self.loop = loop

        self.stopping = False

        utils.log(self.logger, "DEBUG", "Entering run()")

        self.init_sun()

        # Load App Config

        self.app_config = self.read_config()

        # Take a note of DST

        self.was_dst = self.is_dst()

        # Setup sun

        self.update_sun()

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        utils.log(self.logger, "DEBUG", "Creating worker threads ...")

        # Create Worker Threads
        for i in range(self.threads):
            t = threading.Thread(target=self.worker)
            t.daemon = True
            t.start()

        utils.log(self.logger, "DEBUG", "Done")


        # Create timer loop

        utils.log(self.logger, "DEBUG", "Starting timer loop")

        loop.create_task(self.do_every(self.tick, self.do_every_second))

        # Create utility loop

        utils.log(self.logger, "DEBUG", "Starting utility loop")

        loop.create_task(self.utility())

        # Load Plugins

        for name in self.plugin_params:
            basename = self.plugin_params[name]["plugin"]
            module_name = "{}plugin".format(basename)
            class_name = "{}Plugin".format(basename.capitalize())
            basepath = "appdaemon.plugins"

            utils.log(self.logger, "INFO",
                      "Loading Plugin {} using class {} from module {}".format(name, class_name, module_name))
            full_module_name = "{}.{}.{}".format(basepath, basename, module_name)
            mod = __import__(full_module_name, globals(), locals(), [module_name], 0)
            app_class = getattr(mod, class_name)

            plugin = app_class(self, name, self.logger, self.error, self.loglevel, self.plugin_params[name])

            state = plugin.get_complete_state()
            namespace = plugin.get_namespace()

            with self.state_lock:
                self.state[namespace] = state

            if namespace in self.plugins:
                raise ValueError("Duplicate namespace: {}".format(namespace))

            self.plugins[namespace] = plugin

            loop.create_task(plugin.get_updates())

        #
        # All plugins are loaded and we have initial state
        # Now we can initialize the Apps
        #

        utils.log(self.logger, "DEBUG", "Reading Apps")

        self.read_apps(True)
        self.app_config_file_modified = self.now

        utils.log(self.logger, "INFO", "App initialization complete")

        #
        # Fire APPD Started Event
        #
        self.process_event({"event_type": "appd_started", "data": {}})
        #
        # Initialization complete - now we run in the various async routines we added to the loop
        #

    def get_plugin(self, name):
        if name in self.plugins:
            return self.plugins[name]
        else:
            return None

    def _process_arg(self, arg, kwargs):
        if kwargs:
            if arg in kwargs:
                setattr(self, arg, kwargs[arg])

    def stop(self):
        self.stopping = True
        # if ws is not None:
        #    ws.close()
        self.appq.put_nowait({"event_type": "ha_stop", "data": None})
        for plugin in self.plugins:
            self.plugins[plugin].stop()

    #
    # Diagnostics
    #

    def dump_callbacks(self):
        if self.callbacks == {}:
            utils.log(self.logger, "INFO", "No callbacks")
        else:
            utils.log(self.logger, "INFO", "--------------------------------------------------")
            utils.log(self.logger, "INFO", "Callbacks")
            utils.log(self.logger, "INFO", "--------------------------------------------------")
            for name in self.callbacks.keys():
                utils.log(self.logger, "INFO", "{}:".format(name))
                for uuid_ in self.callbacks[name]:
                    utils.log(self.logger, "INFO", "  {} = {}".format(uuid_, self.callbacks[name][uuid_]))
            utils.log(self.logger, "INFO", "--------------------------------------------------")

    def dump_objects(self):
        utils.log(self.logger, "INFO", "--------------------------------------------------")
        utils.log(self.logger, "INFO", "Objects")
        utils.log(self.logger, "INFO", "--------------------------------------------------")
        for object_ in self.objects.keys():
            utils.log(self.logger, "INFO", "{}: {}".format(object_, self.objects[object_]))
        utils.log(self.logger, "INFO", "--------------------------------------------------")

    def dump_queue(self):
        utils.log(self.logger, "INFO", "--------------------------------------------------")
        utils.log(self.logger, "INFO", "Current Queue Size is {}".format(self.q.qsize()))
        utils.log(self.logger, "INFO", "--------------------------------------------------")

    def get_callback_entries(self):
        callbacks = {}
        for name in self.callbacks.keys():
            callbacks[name] = {}
            for uuid_ in self.callbacks[name]:
                callbacks[name][uuid_] = {}
                if "entity" in callbacks[name][uuid_]:
                    callbacks[name][uuid_]["entity"] = self.callbacks[name][uuid_]["entity"]
                else:
                    callbacks[name][uuid_]["entity"] = None
                callbacks[name][uuid_]["type"] = self.callbacks[name][uuid_]["type"]
                callbacks[name][uuid_]["kwargs"] = self.callbacks[name][uuid_]["kwargs"]
                callbacks[name][uuid_]["function"] = self.callbacks[name][uuid_]["function"]
                callbacks[name][uuid_]["name"] = self.callbacks[name][uuid_]["name"]
        return callbacks


    #
    # Constraints

    # TODO: Pull this into the API
    def check_constraint(self, key, value):
        unconstrained = True
        with self.state_lock:
            if key == "constrain_input_boolean":
                values = value.split(",")
                if len(values) == 2:
                    entity = values[0]
                    state = values[1]
                else:
                    entity = value
                    state = "on"
                if entity in self.state and self.state[entity]["state"] != state:
                    unconstrained = False
            if key == "constrain_input_select":
                values = value.split(",")
                entity = values.pop(0)
                if entity in self.state and self.state[entity]["state"] not in values:
                    unconstrained = False
            if key == "constrain_presence":
                if value == "everyone" and not utils.everyone_home():
                    unconstrained = False
                elif value == "anyone" and not utils.anyone_home():
                    unconstrained = False
                elif value == "noone" and not utils.noone_home():
                    unconstrained = False
            if key == "constrain_days":
                if self.today_is_constrained(value):
                    unconstrained = False

        return unconstrained

    def check_time_constraint(self, args, name):
        unconstrained = True
        if "constrain_start_time" in args or "constrain_end_time" in args:
            if "constrain_start_time" not in args:
                start_time = "00:00:00"
            else:
                start_time = args["constrain_start_time"]
            if "constrain_end_time" not in args:
                end_time = "23:59:59"
            else:
                end_time = args["constrain_end_time"]
            if not self.now_is_between(start_time, end_time, name):
                unconstrained = False

        return unconstrained

    def today_is_constrained(self, days):
        day = self.get_now().weekday()
        daylist = [utils.day_of_week(day) for day in days.split(",")]
        if day in daylist:
            return False
        return True

    #
    # Thread Management
    #

    def dispatch_worker(self, name, args):
        unconstrained = True
        #
        # Argument Constraints
        #
        for arg in self.app_config[name].keys():
            if not self.check_constraint(arg, self.app_config[name][arg]):
                unconstrained = False
        if not self.check_time_constraint(self.app_config[name], name):
            unconstrained = False
        #
        # Callback level constraints
        #
        if "kwargs" in args:
            for arg in args["kwargs"].keys():
                if not self.check_constraint(arg, args["kwargs"][arg]):
                    unconstrained = False
            if not self.check_time_constraint(args["kwargs"], name):
                unconstrained = False

        if unconstrained:
            self.q.put_nowait(args)


    # noinspection PyBroadException
    def worker(self):
        while True:
            args = self.q.get()
            _type = args["type"]
            funcref = args["function"]
            _id = args["id"]
            name = args["name"]
            if name in self.objects and self.objects[name]["id"] == _id:
                try:
                    if _type == "initialize":
                        utils.log(self.logger, "DEBUG", "Calling initialize() for {}".format(name))
                        funcref()
                        utils.log(self.logger, "DEBUG", "{} initialize() done".format(name))
                    elif _type == "timer":
                        funcref(utils.sanitize_timer_kwargs(args["kwargs"]))
                    elif _type == "attr":
                        entity = args["entity"]
                        attr = args["attribute"]
                        old_state = args["old_state"]
                        new_state = args["new_state"]
                        funcref(entity, attr, old_state, new_state,
                                utils.sanitize_state_kwargs(args["kwargs"]))
                    elif _type == "event":
                        data = args["data"]
                        funcref(args["event"], data, args["kwargs"])

                except:
                    utils.log(self.error, "WARNING", '-' * 60)
                    utils.log(self.error, "WARNING", "Unexpected error in worker for App {}:".format(name))
                    utils.log(self.error, "WARNING", "Worker Ags: {}".format(args))
                    utils.log(self.error, "WARNING", '-' * 60)
                    utils.log(self.error, "WARNING", traceback.format_exc())
                    utils.log(self.error, "WARNING", '-' * 60)
                    if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                        utils.log(self.logger, "WARNING", "Logged an error to {}".format(self.errorfile))
            else:
                self.logger.warning("Found stale callback for {} - discarding".format(name))

            if self.inits.get(name):
                self.inits.pop(name)

            self.q.task_done()

    #
    # State
    #

    def entity_exists(self, namespace, entity):
        with self.state_lock:
            if namespace in self.state and entity in self.state[namespace]:
                return True
            else:
                return False


    def add_state_callback(self, name, namespace, entity, cb, kwargs):
        with self.callbacks_lock:
            if name not in self.callbacks:
                self.callbacks[name] = {}
            handle = uuid.uuid4()
            self.callbacks[name][handle] = {
                "name": name,
                "id": self.objects[name]["id"],
                "type": "state",
                "function": cb,
                "entity": entity,
                "namespace": namespace,
                "kwargs": kwargs
            }

        #
        # In the case of a quick_start parameter,
        # start the clock immediately if the device is already in the new state
        #
        if "immediate" in kwargs and kwargs["immediate"] is True:
            if entity is not None and "new" in kwargs and "duration" in kwargs:
                with self.state_lock:
                    if self.state[namespace][entity]["state"] == kwargs["new"]:
                        exec_time = self.get_now_ts() + int(kwargs["duration"])
                        kwargs["handle"] = self.insert_schedule(
                            name, exec_time, cb, False, None,
                            entity=entity,
                            attribute=None,
                            old_state=None,
                            new_state=kwargs["new"], **kwargs
                    )

        return handle

    def cancel_state_callback(self, handle, name):
        with self.callbacks_lock:
            if name in self.callbacks and handle in self.callbacks[name]:
                del self.callbacks[name][handle]
            if name in self.callbacks and self.callbacks[name] == {}:
                del self.callbacks[name]

    def info_state_callback(self, handle, name):
        with self.callbacks_lock:
            if name in self.callbacks and handle in self.callbacks[name]:
                callback = self.callbacks[name][handle]
                return (
                    callback["namespace"],
                    callback["entity"],
                    callback["kwargs"].get("attribute", None),
                    utils.sanitize_state_kwargs(callback["kwargs"])
                )
            else:
                raise ValueError("Invalid handle: {}".format(handle))

    def get_state(self, namespace, device, entity, attribute):
            with self.state_lock:
                if device is None:
                    return self.state[namespace]
                elif entity is None:
                    devices = {}
                    for entity_id in self.state[namespace].keys():
                        thisdevice, thisentity = entity_id.split(".")
                        if device == thisdevice:
                            devices[entity_id] = self.state[namespace][entity_id]
                    return devices
                elif attribute is None:
                    entity_id = "{}.{}".format(device, entity)
                    if entity_id in self.state[namespace]:
                        return self.state[namespace][entity_id]["state"]
                    else:
                        return None
                else:
                    entity_id = "{}.{}".format(device, entity)
                    if attribute == "all":
                        if entity_id in self.state[namespace]:
                            return self.state[namespace][entity_id]["attributes"]
                        else:
                            return None
                    else:
                        if attribute in self.state[namespace][entity_id]:
                            return self.state[namespace][entity_id][attribute]
                        elif attribute in self.state[namespace][entity_id]["attributes"]:
                            return self.state[namespace][entity_id]["attributes"][
                                attribute]
                        else:
                            return None

    def set_state(self, namespace, entity, state):
        with self.state_lock:
            self.state[namespace][entity] = state

    def set_app_state(self, entity_id, state):
        utils.log(self.logger, "DEBUG", "set_app_state: {}".format(entity_id))
        if entity_id is not None and "." in entity_id:
            with self.state_lock:
                if entity_id in self.state:
                    old_state = self.state[entity_id]
                else:
                    old_state = None
                data = {"entity_id": entity_id, "new_state": state, "old_state": old_state}
                args = {"event_type": "state_changed", "data": data}
                self.appq.put_nowait(args)

    #
    # Events
    #
    def add_event_callback(self, name, cb, event, **kwargs):
        with self.callbacks_lock:
            if name not in self.callbacks:
                self.callbacks[name] = {}
            handle = uuid.uuid4()
            self.callbacks[name][handle] = {
                "name": name,
                "id": self.objects[name]["id"],
                "type": "event",
                "function": cb,
                "event": event,
                "kwargs": kwargs
            }
        return handle

    def cancel_event_callback(self, name, handle):
        with self.callbacks_lock:
            if name in self.callbacks and handle in self.callbacks[name]:
                del self.callbacks[name][handle]
            if name in self.callbacks and self.callbacks[name] == {}:
                del self.callbacks[name]

    def info_event_callback(self, name, handle):
        with self.callbacks_lock:
            if name in self.callbacks and handle in self.callbacks[name]:
                callback = self.callbacks[name][handle]
                return callback["event"], callback["kwargs"].copy()
            else:
                raise ValueError("Invalid handle: {}".format(handle))

    #
    # Scheduler
    #

    def cancel_timer(self, name, handle):
        utils.log(self.logger, "DEBUG", "Canceling timer for {}".format(name))
        with self.schedule_lock:
            if name in self.schedule and handle in self.schedule[name]:
                del self.schedule[name][handle]
            if name in self.schedule and self.schedule[name] == {}:
                del self.schedule[name]

    # noinspection PyBroadException
    def exec_schedule(self, name, entry, args):
        try:
            # Locking performed in calling function
            if "inactive" in args:
                return
            # Call function
            if "entity" in args["kwargs"]:
                self.dispatch_worker(name, {
                    "name": name,
                    "id": self.objects[name]["id"],
                    "type": "attr",
                    "function": args["callback"],
                    "attribute": args["kwargs"]["attribute"],
                    "entity": args["kwargs"]["entity"],
                    "new_state": args["kwargs"]["new_state"],
                    "old_state": args["kwargs"]["old_state"],
                    "kwargs": args["kwargs"],
                })
            else:
                self.dispatch_worker(name, {
                    "name": name,
                    "id": self.objects[name]["id"],
                    "type": "timer",
                    "function": args["callback"],
                    "kwargs": args["kwargs"],
                })
            # If it is a repeating entry, rewrite with new timestamp
            if args["repeat"]:
                if args["type"] == "next_rising" or args["type"] == "next_setting":
                    # Its sunrise or sunset - if the offset is negative we
                    # won't know the next rise or set time yet so mark as inactive
                    # So we can adjust with a scan at sun rise/set
                    if args["offset"] < 0:
                        args["inactive"] = 1
                    else:
                        # We have a valid time for the next sunrise/set so use it
                        c_offset = self.get_offset(args)
                        args["timestamp"] = self.calc_sun(args["type"]) + c_offset
                        args["offset"] = c_offset
                else:
                    # Not sunrise or sunset so just increment
                    # the timestamp with the repeat interval
                    args["basetime"] += args["interval"]
                    args["timestamp"] = args["basetime"] + self.get_offset(args)
            else:  # Otherwise just delete
                del self.schedule[name][entry]

        except:
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(
                self.error, "WARNING",
                "Unexpected error during exec_schedule() for App: {}".format(name)
            )
            utils.log(self.error, "WARNING", "Args: {}".format(args))
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", traceback.format_exc())
            utils.log(self.error, "WARNING", '-' * 60)
            if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                # When explicitly logging to stdout and stderr, suppress
                # verbose_log messages about writing an error (since they show up anyway)
                utils.log(self.logger, "WARNING", "Logged an error to {}".format(self.errorfile))
            utils.log(self.error, "WARNING", "Scheduler entry has been deleted")
            utils.log(self.error, "WARNING", '-' * 60)

            del self.schedule[name][entry]

    def process_sun(self, action):
        utils.log(
            self.logger, "DEBUG",
            "Process sun: {}, next sunrise: {}, next sunset: {}".format(
                action, self.sun["next_rising"], self.sun["next_setting"]
            )
        )
        with self.schedule_lock:
            for name in self.schedule.keys():
                for entry in sorted(
                        self.schedule[name].keys(),
                        key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                ):
                    schedule = self.schedule[name][entry]
                    if schedule["type"] == action and "inactive" in schedule:
                        del schedule["inactive"]
                        c_offset = self.get_offset(schedule)
                        schedule["timestamp"] = self.calc_sun(action) + c_offset
                        schedule["offset"] = c_offset

    def calc_sun(self, type_):
        # convert to a localized timestamp
        return self.sun[type_].timestamp()

    def info_timer(self, handle, name):
        with self.schedule_lock:
            if name in self.schedule and handle in self.schedule[name]:
                callback = self.schedule[name][handle]
                return (
                    datetime.datetime.fromtimestamp(callback["timestamp"]),
                    callback["interval"],
                    utils.sanitize_timer_kwargs(callback["kwargs"])
                )
            else:
                raise ValueError("Invalid handle: {}".format(handle))

    def init_sun(self):
        latitude = self.latitude
        longitude = self.longitude

        if -90 > latitude < 90:
            raise ValueError("Latitude needs to be -90 .. 90")

        if -180 > longitude < 180:
            raise ValueError("Longitude needs to be -180 .. 180")

        elevation = self.elevation

        self.tz = pytz.timezone(self.time_zone)

        self.location = astral.Location((
            '', '', latitude, longitude, self.tz.zone, elevation
        ))

    def update_sun(self):
        # now = datetime.datetime.now(self.tz)
        now = self.tz.localize(self.get_now())
        mod = -1
        while True:
            try:
                next_rising_dt = self.location.sunrise(
                    (now + datetime.timedelta(days=mod)).date(), local=False
                )
                if next_rising_dt > now:
                    break
            except astral.AstralError:
                pass
            mod += 1

        mod = -1
        while True:
            try:
                next_setting_dt = self.location.sunset(
                    (now + datetime.timedelta(days=mod)).date(), local=False
                )
                if next_setting_dt > now:
                    break
            except astral.AstralError:
                pass
            mod += 1

        old_next_rising_dt = self.sun.get("next_rising")
        old_next_setting_dt = self.sun.get("next_setting")
        self.sun["next_rising"] = next_rising_dt
        self.sun["next_setting"] = next_setting_dt

        if old_next_rising_dt is not None and old_next_rising_dt != self.sun["next_rising"]:
            # dump_schedule()
            self.process_sun("next_rising")
            # dump_schedule()
        if old_next_setting_dt is not None and old_next_setting_dt != self.sun["next_setting"]:
            # dump_schedule()
            self.process_sun("next_setting")
            # dump_schedule()

    @staticmethod
    def get_offset(kwargs):
        if "offset" in kwargs["kwargs"]:
            if "random_start" in kwargs["kwargs"] \
                    or "random_end" in kwargs["kwargs"]:
                raise ValueError(
                    "Can't specify offset as well as 'random_start' or "
                    "'random_end' in 'run_at_sunrise()' or 'run_at_sunset()'"
                )
            else:
                offset = kwargs["kwargs"]["offset"]
        else:
            rbefore = kwargs["kwargs"].get("random_start", 0)
            rafter = kwargs["kwargs"].get("random_end", 0)
            offset = random.randint(rbefore, rafter)
        # verbose_log(conf.logger, "INFO", "sun: offset = {}".format(offset))
        return offset

    def insert_schedule(self, name, utc, callback, repeat, type_, **kwargs):
        with self.schedule_lock:
            if name not in self.schedule:
                self.schedule[name] = {}
            handle = uuid.uuid4()
            utc = int(utc)
            c_offset = self.get_offset({"kwargs": kwargs})
            ts = utc + c_offset
            interval = kwargs.get("interval", 0)

            self.schedule[name][handle] = {
                "name": name,
                "id": self.objects[name]["id"],
                "callback": callback,
                "timestamp": ts,
                "interval": interval,
                "basetime": utc,
                "repeat": repeat,
                "offset": c_offset,
                "type": type_,
                "kwargs": kwargs
            }
            # verbose_log(conf.logger, "INFO", conf.schedule[name][handle])
        return handle

    def get_scheduler_entries(self):
        schedule = {}
        for name in self.schedule.keys():
            schedule[name] = {}
            for entry in sorted(
                    self.schedule[name].keys(),
                    key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
            ):
                schedule[name][entry] = {}
                schedule[name][entry]["timestamp"] = self.schedule[name][entry]["timestamp"]
                schedule[name][entry]["type"] = self.schedule[name][entry]["type"]
                schedule[name][entry]["name"] = self.schedule[name][entry]["name"]
                schedule[name][entry]["basetime"] = self.schedule[name][entry]["basetime"]
                schedule[name][entry]["repeat"] = self.schedule[name][entry]["basetime"]
                schedule[name][entry]["offset"] = self.schedule[name][entry]["basetime"]
                schedule[name][entry]["interval"] = self.schedule[name][entry]["basetime"]
                schedule[name][entry]["kwargs"] = self.schedule[name][entry]["basetime"]
                schedule[name][entry]["callback"] = self.schedule[name][entry]["callback"]
        return schedule

    def is_dst(self):
        return bool(time.localtime(self.get_now_ts()).tm_isdst)

    def get_now(self):
        return datetime.datetime.fromtimestamp(self.now)

    def get_now_ts(self):
        return self.now

    def now_is_between(self, start_time_str, end_time_str, name=None):
        start_time = self.parse_time(start_time_str, name)
        end_time = self.parse_time(end_time_str, name)
        now = self.get_now()
        start_date = now.replace(
            hour=start_time.hour, minute=start_time.minute,
            second=start_time.second
        )
        end_date = now.replace(
            hour=end_time.hour, minute=end_time.minute, second=end_time.second
        )
        if end_date < start_date:
            # Spans midnight
            if now < start_date and now < end_date:
                now = now + datetime.timedelta(days=1)
            end_date = end_date + datetime.timedelta(days=1)
        return start_date <= now <= end_date

    def sunset(self):
        return datetime.datetime.fromtimestamp(self.calc_sun("next_setting"))

    def sunrise(self):
        return datetime.datetime.fromtimestamp(self.calc_sun("next_rising"))

    def parse_time(self, time_str, name=None):
        parsed_time = None
        parts = re.search('^(\d+):(\d+):(\d+)', time_str)
        if parts:
            parsed_time = datetime.time(
                int(parts.group(1)), int(parts.group(2)), int(parts.group(3))
            )
        else:
            if time_str == "sunrise":
                parsed_time = self.sunrise().time()
            elif time_str == "sunset":
                parsed_time = self.sunset().time()
            else:
                parts = re.search(
                    '^sunrise\s*([+-])\s*(\d+):(\d+):(\d+)', time_str
                )
                if parts:
                    if parts.group(1) == "+":
                        parsed_time = (self.sunrise() + datetime.timedelta(
                            hours=int(parts.group(2)), minutes=int(parts.group(3)),
                            seconds=int(parts.group(4))
                        )).time()
                    else:
                        parsed_time = (self.sunrise() - datetime.timedelta(
                            hours=int(parts.group(2)), minutes=int(parts.group(3)),
                            seconds=int(parts.group(4))
                        )).time()
                else:
                    parts = re.search(
                        '^sunset\s*([+-])\s*(\d+):(\d+):(\d+)', time_str
                    )
                    if parts:
                        if parts.group(1) == "+":
                            parsed_time = (self.sunset() + datetime.timedelta(
                                hours=int(parts.group(2)),
                                minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )).time()
                        else:
                            parsed_time = (self.sunset() - datetime.timedelta(
                                hours=int(parts.group(2)),
                                minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )).time()
        if parsed_time is None:
            if name is not None:
                raise ValueError(
                    "{}: invalid time string: {}".format(name, time_str))
            else:
                raise ValueError("invalid time string: {}".format(time_str))
        return parsed_time

    def dump_sun(self):
        utils.log(self.logger, "INFO", "--------------------------------------------------")
        utils.log(self.logger, "INFO", "Sun")
        utils.log(self.logger, "INFO", "--------------------------------------------------")
        utils.log(self.logger, "INFO", self.sun)
        utils.log(self.logger, "INFO", "--------------------------------------------------")

    def dump_schedule(self):
        if self.schedule == {}:
            utils.log(self.logger, "INFO", "Schedule is empty")
        else:
            utils.log(self.logger, "INFO", "--------------------------------------------------")
            utils.log(self.logger, "INFO", "Scheduler Table")
            utils.log(self.logger, "INFO", "--------------------------------------------------")
            for name in self.schedule.keys():
                utils.log(self.logger, "INFO", "{}:".format(name))
                for entry in sorted(
                        self.schedule[name].keys(),
                        key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                ):
                    utils.log(
                        self.logger, "INFO",
                        "  Timestamp: {} - data: {}".format(
                            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(
                                self.schedule[name][entry]["timestamp"]
                            )),
                            self.schedule[name][entry]
                        )
                    )
            utils.log(self.logger, "INFO", "--------------------------------------------------")

    async def do_every(self, period, f):
        t = math.floor(self.get_now_ts())
        count = 0
        t_ = math.floor(time.time())
        while not self.stopping:
            count += 1
            delay = max(t_ + count * period - time.time(), 0)
            await asyncio.sleep(delay)
            t += self.interval
            r = await f(t)
            if r is not None and r != t:
                # print("r: {}, t: {}".format(r,t))
                t = r
                t_ = r
                count = 0

    #
    # Scheduler Loop
    #

    # noinspection PyBroadException,PyBroadException

    async def do_every_second(self, utc):

        try:
            start_time = datetime.datetime.now().timestamp()
            self.now = utc

            # If we have reached endtime bail out

            if self.endtime is not None and self.get_now() >= self.endtime:
                utils.log(self.logger, "INFO", "End time reached, exiting")
                self.stop()

            if self.realtime:
                real_now = datetime.datetime.now().timestamp()
                delta = abs(utc - real_now)
                if delta > 1:
                    utils.log(self.logger, "WARNING",
                              "Scheduler clock skew detected - delta = {} - resetting".format(delta))
                    return real_now

            # Update sunrise/sunset etc.

            self.update_sun()

            # Check if we have entered or exited DST - if so, reload apps
            # to ensure all time callbacks are recalculated

            now_dst = self.is_dst()
            if now_dst != self.was_dst:
                utils.log(
                    self.logger, "INFO",
                    "Detected change in DST from {} to {} -"
                    " reloading all modules".format(self.was_dst, now_dst)
                )
                # dump_schedule()
                utils.log(self.logger, "INFO", "-" * 40)
                await utils.run_in_executor(self.loop, self.executor, self.read_apps, True)
                # dump_schedule()
            self.was_dst = now_dst

            # dump_schedule()

            # test code for clock skew
            # if random.randint(1, 10) == 5:
            #    time.sleep(random.randint(1,20))


            # Process callbacks

            # utils.verbose_log(self.logger, "DEBUG", "Scheduler invoked at {}".format(now))
            with self.schedule_lock:
                for name in self.schedule.keys():
                    for entry in sorted(
                            self.schedule[name].keys(),
                            key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                    ):

                        if self.schedule[name][entry]["timestamp"] <= utc:
                            self.exec_schedule(name, entry, self.schedule[name][entry])
                        else:
                            break
                for k, v in list(self.schedule.items()):
                    if v == {}:
                        del self.schedule[k]

            end_time = datetime.datetime.now().timestamp()

            loop_duration = (int((end_time - start_time) * 1000) / 1000) * 1000
            utils.log(self.logger, "DEBUG", "Scheduler loop compute time: {}ms".format(loop_duration))

            if loop_duration > 900:
                utils.log(self.logger, "WARNING", "Excessive time spent in scheduler loop: {}ms".format(loop_duration))

            return utc

        except:
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", "Unexpected error during do_every_second()")
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", traceback.format_exc())
            utils.log(self.error, "WARNING", '-' * 60)
            if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                # When explicitly logging to stdout and stderr, suppress
                # verbose_log messages about writing an error (since they show up anyway)
                utils.log(
                    self.logger, "WARNING",
                    "Logged an error to {}".format(self.errorfile)
                )

    #
    # Utility Loop
    #

    async def utility(self):
        while not self.stopping:
            start_time = datetime.datetime.now().timestamp()

            try:
                           # Check to see if any apps have changed but only if we have valid state

                await utils.run_in_executor(self.loop, self.executor, self.read_apps)

                # Check to see if config has changed

                await utils.run_in_executor(self.loop, self.executor, self.check_config)

                # Check for thread starvation

                qsize = self.q.qsize()
                if qsize > 0 and qsize % 10 == 0:
                    self.logger.warning("Queue size is {}, suspect thread starvation".format(self.q.qsize()))

                # Plugins

                for plugin in self.plugins:
                    self.plugins[plugin].utility()

            except:
                utils.log(self.error, "WARNING", '-' * 60)
                utils.log(self.error, "WARNING", "Unexpected error during utility()")
                utils.log(self.error, "WARNING", '-' * 60)
                utils.log(self.error, "WARNING", traceback.format_exc())
                utils.log(self.error, "WARNING", '-' * 60)
                if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                    # When explicitly logging to stdout and stderr, suppress
                    # verbose_log messages about writing an error (since they show up anyway)
                    utils.log(
                        self.logger, "WARNING",
                        "Logged an error to {}".format(self.errorfile)
                    )

            end_time = datetime.datetime.now().timestamp()

            loop_duration = (int((end_time - start_time) * 1000) / 1000) * 1000

            utils.log(self.logger, "DEBUG", "Util loop compute time: {}ms".format(loop_duration))

            if loop_duration > (self.utility_delay * 1000 * 0.9):
                utils.log(self.logger, "WARNING", "Excessive time spent in utility loop: {}ms".format(loop_duration))

            await asyncio.sleep(self.utility_delay)

    #
    # AppDaemon API
    #

    def register_endpoint(self, cb, name):

        handle = uuid.uuid4()

        with self.endpoints_lock:
            if name not in self.endpoints:
                self.endpoints[name] = {}
            self.endpoints[name][handle] = {"callback": cb, "name": name}

        return handle

    def unregister_endpoint(self, handle, name):
        with self.endpoints_lock:
            if name in self.endpoints and handle in self.endpoints[name]:
                del self.endpoints[name][handle]

    #
    # App Management
    #

    def get_app(self, name):
        if name in self.objects:
            return self.objects[name]["object"]
        else:
            return None

    def term_file(self, name):
        for key in self.app_config:
            if "module" in self.app_config[key] and self.app_config[key]["module"] == name:
                self.term_object(key)

    def clear_file(self, name):
        for key in self.app_config:
            if "module" in self.app_config[key] and self.app_config[key]["module"] == name:
                self.clear_object(key)
                if key in self.objects:
                    del self.objects[key]

    def clear_object(self, object_):
        utils.log(self.logger, "DEBUG", "Clearing callbacks for {}".format(object_))
        with self.callbacks_lock:
            if object_ in self.callbacks:
                del self.callbacks[object_]
        with self.schedule_lock:
            if object_ in self.schedule:
                del self.schedule[object_]
        with self.endpoints_lock:
            if object_ in self.endpoints:
                del self.endpoints[object_]

    def term_object(self, name):
        if name in self.objects and hasattr(self.objects[name]["object"], "terminate"):
            utils.log(self.logger, "INFO", "Terminating Object {}".format(name))
            # Call terminate directly rather than via worker thread
            # so we know terminate has completed before we move on
            self.objects[name]["object"].terminate()

    def init_object(self, name, class_name, module_name, args):
        utils.log(self.logger, "INFO",
                  "Loading Object {} using class {} from module {}".format(name, class_name, module_name))
        modname = __import__(module_name)
        app_class = getattr(modname, class_name)
        self.objects[name] = {
            "object": app_class(
                self, name, self.logger, self.error, args, self.config, self.global_vars
            ),
            "id": uuid.uuid4()
        }

        # Call it's initialize function

        self.objects[name]["object"].initialize()

        # with self.threads_busy_lock:
        #     inits[name] = 1
        #     self.threads_busy += 1
        #     q.put_nowait({
        #         "type": "initialize",
        #         "name": name,
        #         "id": self.objects[name]["id"],
        #         "function": self.objects[name]["object"].initialize
        #     })

    def read_config(self):
        new_config = None
        root, ext = os.path.splitext(self.app_config_file)
        with open(self.app_config_file, 'r') as yamlfd:
            config_file_contents = yamlfd.read()
        try:
            new_config = yaml.load(config_file_contents)
        except yaml.YAMLError as exc:
            utils.log(self.logger, "WARNING", "Error loading configuration")
            if hasattr(exc, 'problem_mark'):
                if exc.context is not None:
                    utils.log(self.error, "WARNING", "parser says")
                    utils.log(self.error, "WARNING", str(exc.problem_mark))
                    utils.log(self.error, "WARNING", str(exc.problem) + " " + str(exc.context))
                else:
                    utils.log(self.error, "WARNING", "parser says")
                    utils.log(self.error, "WARNING", str(exc.problem_mark))
                    utils.log(self.error, "WARNING", str(exc.problem))

        return new_config

    # noinspection PyBroadException
    def check_config(self):

        try:
            modified = os.path.getmtime(self.app_config_file)
            if modified > self.app_config_file_modified:
                utils.log(self.logger, "INFO", "{} modified".format(self.app_config_file))
                self.app_config_file_modified = modified
                new_config = self.read_config()

                if new_config is None:
                    utils.log(self.error, "WARNING", "New config not applied")
                    return

                # Check for changes

                for name in self.app_config:
                    # if name == "DEFAULT" or name == "AppDaemon" or name == "HADashboard":
                    #    continue
                    if name in new_config:
                        if self.app_config[name] != new_config[name]:
                            # Something changed, clear and reload

                            utils.log(self.logger, "INFO", "App '{}' changed - reloading".format(name))
                            self.term_object(name)
                            self.clear_object(name)
                            self.init_object(
                                name, new_config[name]["class"],
                                new_config[name]["module"], new_config[name]
                            )
                    else:

                        # Section has been deleted, clear it out

                        utils.log(self.logger, "INFO", "App '{}' deleted - removing".format(name))
                        self.clear_object(name)

                for name in new_config:
                    if name == "DEFAULT" or name == "AppDaemon":
                        continue
                    if name not in self.app_config:
                        #
                        # New section added!
                        #
                        utils.log(self.logger, "INFO", "App '{}' added - running".format(name))
                        self.init_object(
                            name, new_config[name]["class"],
                            new_config[name]["module"], new_config[name]
                        )

                self.app_config = new_config
        except:
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", "Unexpected error:")
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", traceback.format_exc())
            utils.log(self.error, "WARNING", '-' * 60)
            if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                utils.log(self.logger, "WARNING", "Logged an error to {}".format(self.errorfile))

    # noinspection PyBroadException
    def read_app(self, file, reload=False):
        name = os.path.basename(file)
        module_name = os.path.splitext(name)[0]
        # Import the App
        try:
            if reload:
                utils.log(self.logger, "INFO", "Reloading Module: {}".format(file))

                file, ext = os.path.splitext(name)

                #
                # Clear out callbacks and remove objects
                #
                self.term_file(file)
                self.clear_file(file)
                #
                # Reload
                #
                try:
                    importlib.reload(self.modules[module_name])
                except KeyError:
                    if name not in sys.modules:
                        # Probably failed to compile on initial load
                        # so we need to re-import
                        self.read_app(file)
                    else:
                        # A real KeyError!
                        raise
            else:
                utils.log(self.logger, "INFO", "Loading Module: {}".format(file))
                self.modules[module_name] = importlib.import_module(module_name)

            # Instantiate class and Run initialize() function

            if self.app_config is not None:
                for name in self.app_config:
                    if name == "DEFAULT" or name == "AppDaemon" or name == "HASS" or name == "HADashboard":
                        continue
                    if module_name == self.app_config[name]["module"]:
                        class_name = self.app_config[name]["class"]

                        self.init_object(name, class_name, module_name, self.app_config[name])

        except:
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", "Unexpected error during loading of {}:".format(name))
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", traceback.format_exc())
            utils.log(self.error, "WARNING", '-' * 60)
            if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                utils.log(self.logger, "WARNING", "Logged an error to {}".format(self.errorfile))

    def get_module_dependencies(self, file):
        module_name = self.get_module_from_path(file)
        if self.app_config is not None:
            for key in self.app_config:
                if "module" in self.app_config[key] and self.app_config[key]["module"] == module_name:
                    if "dependencies" in self.app_config[key]:
                        return self.app_config[key]["dependencies"].split(",")
                    else:
                        return None

        return None

    def in_previous_dependencies(self, dependencies, load_order):
        for dependency in dependencies:
            dependency_found = False
            for batch in load_order:
                for mod in batch:
                    module_name = self.get_module_from_path(mod["name"])
                    # print(dependency, module_name)
                    if dependency == module_name:
                        # print("found {}".format(module_name))
                        dependency_found = True
            if not dependency_found:
                return False

        return True

    def dependencies_are_satisfied(self, _module, load_order):
        dependencies = self.get_module_dependencies(_module)

        if dependencies is None:
            return True

        if self.in_previous_dependencies(dependencies, load_order):
            return True

        return False

    @staticmethod
    def get_module_from_path(path):
        name = os.path.basename(path)
        module_name = os.path.splitext(name)[0]
        return module_name

    def find_dependent_modules(self, mod):
        module_name = self.get_module_from_path(mod["name"])
        dependents = []
        if self.app_config is not None:
            for mod in self.app_config:
                if "dependencies" in self.app_config[mod]:
                    for dep in self.app_config[mod]["dependencies"].split(","):
                        if dep == module_name:
                            dependents.append(self.app_config[mod]["module"])
        return dependents

    def get_file_from_module(self, mod):
        for file in self.monitored_files:
            module_name = self.get_module_from_path(file)
            if module_name == mod:
                return file

        return None

    @staticmethod
    def file_in_modules(file, modules):
        for mod in modules:
            if mod["name"] == file:
                return True
        return False

    # noinspection PyBroadException
    def read_apps(self, all_=False):
        # Check if the apps are disabled in config
        if not self.apps:
            return

        found_files = []
        modules = []
        for root, subdirs, files in os.walk(self.app_dir):
            if root[-11:] != "__pycache__":
                for file in files:
                    if file[-3:] == ".py":
                        found_files.append(os.path.join(root, file))
        for file in found_files:
            if file == os.path.join(self.app_dir, "__init__.py"):
                continue
            if file == os.path.join(self.app_dir, "__pycache__"):
                continue
            modified = os.path.getmtime(file)
            if file in self.monitored_files:
                if self.monitored_files[file] < modified or all_:
                    # read_app(file, True)
                    thismod = {"name": file, "reload": True, "load": True}
                    modules.append(thismod)
                    self.monitored_files[file] = modified
            else:
                # read_app(file)
                modules.append({"name": file, "reload": False, "load": True})
                self.monitored_files[file] = modified

        # Add any required dependent files to the list

        if modules:
            more_modules = True
            while more_modules:
                module_list = modules.copy()
                for mod in module_list:
                    dependent_modules = self.find_dependent_modules(mod)
                    if not dependent_modules:
                        more_modules = False
                    else:
                        for thismod in dependent_modules:
                            file = self.get_file_from_module(thismod)

                            if file is None:
                                utils.log(self.logger, "ERROR",
                                          "Unable to resolve dependencies due to incorrect references")
                                utils.log(self.logger, "ERROR", "The following modules have unresolved dependencies:")
                                utils.log(self.logger, "ERROR", self.get_module_from_path(mod["file"]))
                                raise ValueError("Unresolved dependencies")

                            mod_def = {"name": file, "reload": True, "load": True}
                            if not self.file_in_modules(file, modules):
                                # print("Appending {} ({})".format(mod, file))
                                modules.append(mod_def)

        # Loading order algorithm requires full population of modules
        # so we will add in any missing modules but mark them for not loading

        for file in self.monitored_files:
            if not self.file_in_modules(file, modules):
                modules.append({"name": file, "reload": False, "load": False})

        # Figure out loading order

        # for mod in modules:
        #  print(mod["name"], mod["load"])

        load_order = []

        while modules:
            batch = []
            module_list = modules.copy()
            for mod in module_list:
                # print(module)
                if self.dependencies_are_satisfied(mod["name"], load_order):
                    batch.append(mod)
                    modules.remove(mod)

            if not batch:
                utils.log(self.logger, "ERROR",
                          "Unable to resolve dependencies due to incorrect or circular references")
                utils.log(self.logger, "ERROR", "The following modules have unresolved dependencies:")
                for mod in modules:
                    module_name = self.get_module_from_path(mod["name"])
                    utils.log(self.logger, "ERROR", module_name)
                raise ValueError("Unresolved dependencies")

            load_order.append(batch)

        try:
            for batch in load_order:
                for mod in batch:
                    if mod["load"]:
                        self.read_app(mod["name"], mod["reload"])

        except:
            utils.log(self.logger, "WARNING", '-' * 60)
            utils.log(self.logger, "WARNING", "Unexpected error loading file")
            utils.log(self.logger, "WARNING", '-' * 60)
            utils.log(self.logger, "WARNING", traceback.format_exc())
            utils.log(self.logger, "WARNING", '-' * 60)

    #
    # State Updates
    #

    def check_and_disapatch(self, name, funcref, entity, attribute, new_state,
                            old_state, cold, cnew, kwargs):
        if attribute == "all":
            self.dispatch_worker(name, {
                "name": name,
                "id": self.objects[name]["id"],
                "type": "attr",
                "function": funcref,
                "attribute": attribute,
                "entity": entity,
                "new_state": new_state,
                "old_state": old_state,
                "kwargs": kwargs
            })
        else:
            if old_state is None:
                old = None
            else:
                if attribute in old_state:
                    old = old_state[attribute]
                elif 'attributes' in old_state and attribute in old_state['attributes']:
                    old = old_state['attributes'][attribute]
                else:
                    old = None
            if new_state is None:
                new = None
            else:
                if attribute in new_state:
                    new = new_state[attribute]
                elif 'attributes' in new_state and attribute in new_state['attributes']:
                    new = new_state['attributes'][attribute]
                else:
                    new = None

            if (cold is None or cold == old) and (cnew is None or cnew == new):
                if "duration" in kwargs:
                    # Set a timer
                    exec_time = self.get_now_ts() + int(kwargs["duration"])
                    kwargs["handle"] = self.insert_schedule(
                        name, exec_time, funcref, False, None,
                        entity=entity,
                        attribute=attribute,
                        old_state=old,
                        new_state=new, **kwargs
                    )
                # Do it now
                self.dispatch_worker(name, {
                    "name": name,
                    "id": self.objects[name]["id"],
                    "type": "attr",
                    "function": funcref,
                    "attribute": attribute,
                    "entity": entity,
                    "new_state": new,
                    "old_state": old,
                    "kwargs": kwargs
                })
            else:
                if "handle" in kwargs:
                    # cancel timer
                    self.cancel_timer(name, kwargs["handle"])

    def process_state_change(self, namespace, state):
        data = state["data"]
        entity_id = data['entity_id']
        utils.log(self.logger, "DEBUG", data)
        device, entity = entity_id.split(".")

        # Process state callbacks

        with self.callbacks_lock:
            for name in self.callbacks.keys():
                for uuid_ in self.callbacks[name]:
                    callback = self.callbacks[name][uuid_]
                    if callback["type"] == "state" and callback["namespace"] == namespace:
                        cdevice = None
                        centity = None
                        if callback["entity"] is not None:
                            if "." not in callback["entity"]:
                                cdevice = callback["entity"]
                                centity = None
                            else:
                                cdevice, centity = callback["entity"].split(".")
                        if callback["kwargs"].get("attribute") is None:
                            cattribute = "state"
                        else:
                            cattribute = callback["kwargs"].get("attribute")

                        cold = callback["kwargs"].get("old")
                        cnew = callback["kwargs"].get("new")

                        if cdevice is None:
                            self.check_and_disapatch(
                                name, callback["function"], entity_id,
                                cattribute,
                                data['new_state'],
                                data['old_state'],
                                cold, cnew,
                                callback["kwargs"]
                            )
                        elif centity is None:
                            if device == cdevice:
                                self.check_and_disapatch(
                                    name, callback["function"], entity_id,
                                    cattribute,
                                    data['new_state'],
                                    data['old_state'],
                                    cold, cnew,
                                    callback["kwargs"]
                                )
                        elif device == cdevice and entity == centity:
                            self.check_and_disapatch(
                                name, callback["function"], entity_id,
                                cattribute,
                                data['new_state'],
                                data['old_state'], cold,
                                cnew,
                                callback["kwargs"]
                            )

    def state_update(self, namespace, data):
        try:
            utils.log(
                self.logger, "DEBUG",
                "Event type:{}:".format(data['event_type'])
            )
            utils.log(self.logger, "DEBUG", data["data"])

            if data['event_type'] == "state_changed":
                entity_id = data['data']['entity_id']

                # First update our global state
                with self.state_lock:
                    self.state[namespace][entity_id] = data['data']['new_state']

            if self.apps is True:
                # Process state changed message
                if data['event_type'] == "state_changed":
                    self.process_state_change(namespace, data)

                # Process non-state callbacks
                self.process_event(data)

            # Update dashboards

            #if self.dashboard is True:
            #    appdash.ws_update(data)

        except:
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", "Unexpected error during state_update()")
            utils.log(self.error, "WARNING", '-' * 60)
            utils.log(self.error, "WARNING", traceback.format_exc())
            utils.log(self.error, "WARNING", '-' * 60)
            if self.errorfile != "STDERR" and self.logfile != "STDOUT":
                utils.log(self.logger, "WARNING", "Logged an error to {}".format(self.errorfile))


    #
    # Event Update
    #

    def process_event(self, data):
        with self.callbacks_lock:
            for name in self.callbacks.keys():
                for uuid_ in self.callbacks[name]:
                    callback = self.callbacks[name][uuid_]
                    if "event" in callback and (
                                    callback["event"] is None
                            or data['event_type'] == callback["event"]):
                        # Check any filters
                        _run = True
                        for key in callback["kwargs"]:
                            if key in data["data"] and callback["kwargs"][key] != \
                                    data["data"][key]:
                                _run = False
                        if _run:
                            self.dispatch_worker(name, {
                                "name": name,
                                "id": self.objects[name]["id"],
                                "type": "event",
                                "event": data['event_type'],
                                "function": callback["function"],
                                "data": data["data"],
                                "kwargs": callback["kwargs"]
                            })

    #
    # Plugin Management
    #

    def get_plugin(self, name):
        return self.plugins[name]
