import logging
import logging.config
import json
import os
import sys
import traceback
import time
import threading

from src import config
from src.parsers import Suricata, Snort, PaloAltoParser, PA
from src.notify import Notification
from src.filtering import AlertFilter
from src.misc.utils import get_hostname
from src.misc.restart import detect_change
from src.abstraction.models import Alert

# Vars needed when 'restart' is enabled
sys_args = sys.argv
sys_exe = sys.executable

# Create logger
logging.config.dictConfig(config.logging)
logger = logging.getLogger("alertBot")

log_levels = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "debug": logging.DEBUG
}

if len(sys_args) > 1 and sys_args[1] != "restarted":
    try:
        # Use log level from command line argument
        logger.setLevel(log_levels[sys_args[1]])
    except KeyError as e:
        print(e)
        logger.error(e)
        logger.warning("Not a valid log level!")
        logger.warning(f"Valid log levels are: {log_levels.keys()}")
        exit(1)
    except IndexError as IE:
        pass

# File state for alerts (global)
state_file = "fileState.json"
if not os.path.isfile(state_file) or os.path.getsize(state_file) == 0:
    # Create the file_state file with default values
    logger.info("fileState.json do not exist or is empty. Creating default..")

    default_state = {}
    for _sensor in config["sensors"].keys():
        default_state[_sensor] = {}
        default_state[_sensor][config.sensors[_sensor].interface] = 0

    with open(state_file, "w") as file:
        json.dump(default_state, file)

alert_filter = None  # AlertFilter cls
filter_list = None  # JSON loaded filter list
isFilter_enabled = config.filter.enabled
isNotify_enabled = config.notify.enabled
isNotifyOnStartUp_enabled = config.notify.notifyOnStartUp
# isPcapParser_enabled = config.pcapParser.enabled
isReverseDNS_enabled = config.general.reverseDns
isRestartOnChange_enabled = config.general.restartOnChange

logger.info(f"Reverse DNS is {isReverseDNS_enabled}")
# logger.info(f"Pcap Parser is {isPcapParser_enabled}")
logger.info(f"Notification is {isNotify_enabled}")
logger.info(f"Startup alert is {isNotifyOnStartUp_enabled}")
logger.info(f"Filter is {isFilter_enabled}")
logger.info(f"Restart on change is {isRestartOnChange_enabled}")

if isFilter_enabled:
    if not os.path.isfile("filter.json"):
        logger.warning("filter.json do not exist")

        exit("Filter is enabled but does not exist.. Create file 'filter.json'")

    with open(config.filter.path, encoding="utf-8") as f:
        try:
            filter_list = json.load(f)
        except json.decoder.JSONDecodeError as je:
            logger.error("Error in 'filter.json' -> %s", je)
            logger.exception(msg="Must escape 'escape' chars in regex in 'filter.json' bc json... "
                                 "Usually solved by '\\\\'", exc_info=je)
            exit(1)

    alert_filter = AlertFilter(filter_list)

restart_success = False
if sys_args[len(sys_args) - 1] == "restarted":
    logger.info("AlertBot Restarted Successfully")
    restart_success = True
    sys_args.pop()

notify = None
if isNotify_enabled:
    notify = Notification(config.notify)
    if isNotifyOnStartUp_enabled and restart_success:
        notify.send_notification(message="Started after successful restart", title="Restart event")
        # Not really necessary I think..
        restart_success = False

    elif isNotifyOnStartUp_enabled:
        notify.send_notification(message="AlertBot powered up..", title="Upstart event")


# parsers Dictionary holds all available parser classes and parser functions. Could be auto generated but meh.
parsers = {
    "snort": {
        "parser_cls": Snort,
        "logType": {
            "full": "full_log",  # The value is the name of the parser function in the parser class
            "fast": "fast_log"
        }
    },
    "suricata": {
        "parser_cls": Suricata,
        "logType": {
            "eve": "eve_json",  # The value is the name of the parser function in the parser class
            "fast": "fast_log",
            "full": "full_log"
        }
    },
    "paloalto": {
        "parser_cls": PaloAltoParser,
        "logType": {
            "threat": "threat_log"
        }
    }
}


def get_enabled_sensor():
    """ Get the enabled sensor config """
    enabled_count = 0
    selected_sensor = None
    for sensor in config.sensors:
        if config.sensors[sensor].enabled:
            enabled_count += 1
            # Creates a new key "sensorType" = sensor ex snort
            config.sensors[sensor]["sensorType"] = sensor
            selected_sensor = config.sensors[sensor]

    if not selected_sensor:
        logger.error("No sensors is enabled.. Plz enable ONE!")
        exit(1)

    if enabled_count > 1:
        logger.error(f"{enabled_count} sensor enabled! Only one can be enabled!")
        exit(1)

    return selected_sensor


def get_logfile_state() -> dict:
    """ Get log state """
    # Get last file position for log files.. 'state_file' is global
    with open(state_file, "r") as s_file:
        state = json.load(s_file)

    return state


def save_logfile_state(new_state: int, sensor: str, interface: str):
    """ Save log state """
    # Save current file position of logfile. 'state_file' is global
    new_file_state = get_logfile_state()
    # Update file state
    new_file_state[sensor][interface] = new_state
    with open(state_file, "w") as s_file:
        json.dump(new_file_state, s_file)


def tail_file(logfile, parser, sensor_name: str, interface: str):
    """ Tail file log source """
    # Let's figure out where we left off
    saved_file_poss = get_logfile_state()[sensor_name][interface]
    current_file_poss = 0
    if saved_file_poss > current_file_poss:
        current_file_poss = saved_file_poss
    logger.info(f"Sensor: {sensor_name.title()} Interface: {interface},  Alert file position start up:"
                f" {current_file_poss} (file position)")

    while True:
        # While loop runs as long as threding.Event() is set -> run_tail. Only used in threading
        logfile.seek(0, 2)
        if logfile.tell() < current_file_poss:
            logfile.seek(0, 0)
        else:
            logfile.seek(current_file_poss, 0)
        line = logfile.readline()
        if not line:
            time.sleep(1)
            # Sleep 1 sec and jumps to 'next' while loop iterations
            continue

        if current_file_poss < logfile.tell():
            logger.debug(f"This is where we are at {logfile.tell()}, in file {interface}")

        # Lets run the new line through the parser and see whats happening before we say we have read it..
        logger.debug(line)

        parsed_line = parser(line)
        # alert = parsed_line
        if not parsed_line:
            # BC eve.json lines may contain other event types than alert. Parser(s) handles this..
            # Update current file state
            current_file_poss = logfile.tell()
            # Save current read position state to file
            save_logfile_state(new_state=current_file_poss, sensor=sensor_name, interface=interface)
            # Jumps to 'next' while loop iterations
            continue

        alert = Alert(**parsed_line)
        # Add interface to alert
        alert.interface = interface

        if not isFilter_enabled and isNotify_enabled:
            # Notifications is enabled but we are not filtering any alert..
            logger.debug("Sending notification..")

            notify.send_notification(
                message=alert.__dict__, title=f"{sensor_name} Event".title()
            )

        if isFilter_enabled and not alert_filter.run_filter(alert=alert):
            # Filter is enabled and this alert did not match any filters so we can send notification

            # Get pcap if enabled. Only applicable for PFsnort alerts
            # if isPcapParser_enabled:
            #     # Returns url to view pcap -> str
            #     pcap_url = get_alert_pcap(alert.__dict__)
            #     alert.pcap = pcap_url

            # Add reverse DNS to src/dest
            if isReverseDNS_enabled:
                # Reverse DNS is slow
                src_dns = get_hostname(alert.src)
                formatted_src = alert.src + " - " + str(src_dns)
                dest_dns = get_hostname(alert.dest)
                formatted_dest = alert.dest + " - " + str(dest_dns)

                alert.src = formatted_src
                alert.dest = formatted_dest

            if isNotify_enabled:
                logger.debug("Sending notification..")

                notify.send_notification(
                    message=alert.__dict__, title=f"{sensor_name} Event".title()
                )

        # Update current file state
        current_file_poss = logfile.tell()
        # save current file state to file
        save_logfile_state(new_state=current_file_poss, sensor=sensor_name, interface=interface)


def tail_http(parser, sensor_name: str, interface: str, sensor_conf):
    """ Tail http 'log source'
        Should prolly be normalize if adding more http sources
    """
    from pan.xapi import PanXapiError
    pull_interval = sensor_conf.pullInterval * 60  # 2*60  # minutes

    # Init PA API class
    try:
        palo = PA(ip=sensor_conf.ip, port=sensor_conf.port, apikey=sensor_conf.apikey)
    except PanXapiError as msg:
        logger.error('pan.xapi.PanXapi: %s', msg)
        exit(1)
    # palo = PA(**sensor_conf)

    saved_seqno = get_logfile_state()[sensor_name][interface]

    current_seqno = 0
    if saved_seqno > current_seqno:
        current_seqno = saved_seqno

    logger.info(f"Sensor: {sensor_name.title()} Interface: {interface},  Alert seqNo start up:"
                f" {current_seqno}")

    _filter = "( seqno geq {current_seqno} ) and !( seqno eq {current_seqno} )"
    # ( seqno geq 30334 ) and !( seqno eq 30334 )
    # filter=f"( seqno geq 13737 ) and !( seqno eq 13737 )"

    while True:
        logger.debug(f"PA query filter '{_filter.format(current_seqno=current_seqno)}'")
        # Query PA for logs
        palo.log(log_type=sensor_conf.logType, nlogs=sensor_conf.nlogs, filter=_filter.format(current_seqno=current_seqno))
        json_res = palo.query_result()

        try:
            logs = json_res["response"]["result"]["log"]["logs"]["entry"]
        except KeyError:
            # No new alerts..
            logger.debug("No new logs from PA..")
            logs = []

        logger.debug(f"Total result from PA query: {len(logs)}")
        # parsed_alerts = parser(logs)
        # must sort received logs in order to 'increment' var current_seqno
        # else we're gonna get the same alerts multiple times..
        parsed_alerts = sorted(parser(logs), key=lambda s: s["seqno"], reverse=False)

        for a in parsed_alerts:
            alert = Alert(**a)

            alert.interface = interface

            if not isFilter_enabled and isNotify_enabled:
                # Notifications is enabled but we are not filtering any alert..
                logger.debug("Sending notification..")

                notify.send_notification(
                    message=alert.__dict__, title=f"{sensor_name} Event".title()
                )

            if isFilter_enabled and not alert_filter.run_filter(alert=alert):
                # Filter is enabled and this alert did not match any filters so we can send notification

                # Add reverse DNS to src/dest
                if isReverseDNS_enabled:
                    # Reverse DNS is slow
                    src_dns = get_hostname(alert.src)
                    formatted_src = alert.src + " - " + str(src_dns)
                    dest_dns = get_hostname(alert.dest)
                    formatted_dest = alert.dest + " - " + str(dest_dns)

                    alert.src = formatted_src
                    alert.dest = formatted_dest

                if isNotify_enabled:
                    logger.debug("Sending notification..")

                    notify.send_notification(
                        message=alert.__dict__, title=f"{sensor_name} Event".title()
                    )

            # Update current seqno
            current_seqno = alert.seqno  # int(a["seqno"])

        # save current file state to file
        save_logfile_state(new_state=current_seqno, sensor=sensor_name, interface=interface)

        # Sleep
        time.sleep(pull_interval)


if __name__ == "__main__":
    logger.info("Powering up...")

    # tail_run determines while loop in tail() to run. Dont known how to do this another way..
    # Only needed in a threading setup..
    # tail_run = threading.Event()
    # tail_run.set()
    threads = []
    run_event = None
    if isRestartOnChange_enabled:
        watch_interval = config.general.watchInterval  # Minutes
        watched_files = config.general.watchedFiles
        run_event = threading.Event()
        run_event.set()
        th = threading.Thread(target=detect_change, args=(sys_exe, sys_args, run_event, watch_interval,
                                                          watched_files, alert_filter))
        threads.append(th)
        th.start()

    # Get the enabled sensor config
    enabled_sensor_cfg = get_enabled_sensor()

    active_sensor = enabled_sensor_cfg.sensorType
    sensor_interface = enabled_sensor_cfg.interface

    # If for some reason the sensor interface change after first run, we need to update..
    current = get_logfile_state()
    try:
        current[active_sensor][sensor_interface]
    except KeyError:
        # Add the new interface to state_file
        save_logfile_state(new_state=0, sensor=active_sensor, interface=sensor_interface)

    # Init selected parser class and parser function
    parser_cls = parsers[enabled_sensor_cfg.sensorType]["parser_cls"]()
    parser_func = parsers[enabled_sensor_cfg.sensorType]["logType"][enabled_sensor_cfg.logType]
    # parser ->This is the same as doing Snort().full_log if snort is the enabled sensor and full_log is the function.
    parser = getattr(parser_cls, parser_func)

    log_source_type = None

    if enabled_sensor_cfg.logSourceType == "file":
        log_source_type = "file"
    else:
        log_source_type = "http"

    # Catch keyboard interrupt so we can stop tail() while loop. This kills the while loop, nothing pretty about it..
    try:
        if log_source_type == "file":
            # Open log file. This file object will stay open until KeyboardInterrupt is caught (or killed).
            alert_file = open(enabled_sensor_cfg.filePath, "r")
            tail_file(logfile=alert_file, parser=parser, sensor_name=active_sensor,
                      interface=sensor_interface)
        else:
            tail_http(parser=parser, sensor_name=active_sensor, interface=sensor_interface, sensor_conf=enabled_sensor_cfg)

    except KeyboardInterrupt:
        # Kill tail() while loop. KeyboardInterrupt is the real killer of tail()
        # tail_run.clear()
        logger.info("Killed tail()..")
        if log_source_type == "file":
            file_position = alert_file.tell()
            alert_file.close()
            logger.info(f"Closed logfile '{enabled_sensor_cfg.filePath}'")

            save_logfile_state(new_state=file_position, sensor=active_sensor, interface=sensor_interface)
            logger.info(f"Saved current state: {file_position} (file position)")
        else:
            # logSourceType is HTTP
            pass
        # Get current position in log file and clean up
        # file_position = alert_file.tell()
        # alert_file.close()
        # logger.info(f"Closed logfile '{enabled_sensor_cfg.filePath}'")
        #
        # save_logfile_state(new_state=file_position, sensor=active_sensor, interface=sensor_interface)
        # logger.info(f"Saved current state: {file_position} (file position)")

        if isFilter_enabled:
            logger.info("Filter stats:")
            logger.info("Check alert filter stats in 'filter_stats.json'")
            alert_filter.save_filter_stats()

        # Kill threads if any (ex file watcher).
        if threads:
            logger.info(f"Killing thread(s).. You must wait 'watchInterval' time. ({config.general.watchInterval}sec)")
            run_event.clear()
            for t in threads:
                t.join(config.general.watchInterval)
            logger.debug("All threads are dead")

        logger.info("Exiting..")
        logging.shutdown()
        exit(0)

    except Exception as e:
        # Catch and Log all unexpected exceptions..
        exc_type, exc_value, exc_traceback = sys.exc_info()
        logger.exception("An unexpected error has occurred!", exc_info=True)

        tb = traceback.format_exception(exc_type, exc_value, exc_traceback)
        msg = "An unexpected error has occurred! Check logs! Shutting down..\n" + "".join(tb_line for tb_line in tb)

        if isNotify_enabled:
            notify.send_notification(message=msg, title="Unexpected error Event")

        # Try to shutdown stuff
        if log_source_type == "file":
            # Get current position in log file and clean up
            alert_file.close()
            logger.info(f"Closed logfile '{enabled_sensor_cfg.filePath}'")
        # No reason to save current file state/poss since that prolly were errors occurs..

        if isFilter_enabled:
            logger.info("Check alert filter stats in 'filter_stats.json'")
            alert_filter.save_filter_stats()

        # Kill threads if any (ex file watcher).
        if threads:
            logger.info(
                f"Killing thread(s).. You must wait 'watchInterval' time. ({config.general.watchInterval}sec)")
            run_event.clear()
            for t in threads:
                t.join(config.general.watchInterval)
            logger.debug("All threads are dead")

        logger.info("Exiting..")
        logging.shutdown()
        exit(1)


