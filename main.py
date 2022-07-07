import yaml
import time
import queue
import re
from threading import Thread
from getpass import getpass
from sys import argv
from datetime import datetime
from pathlib import Path
from netmiko import ConnectHandler
from netmiko.ssh_exception import NetMikoTimeoutException, SSHException

# IOS XR
import logging
logging.getLogger('paramiko.transport').disabled = True

#######################################################################################
# ------------------------------ classes part ----------------------------------------#
#######################################################################################


class NetworkDevice:
    def __init__(self, ip, host):
        self.ip_address = ip
        self.hostname = host
        self.os_type = None
        self.cmd_logs = []
        self.conf_logs = []
        self.connection_status = True   # False if connection fails
        self.connection_error_msg = ""  # connection error message
        self.ssh_conn = None

    def show_commands(self, cmd):
        self.cmd_logs.append(self.ssh_conn.send_command(cmd, strip_command=False, strip_prompt=False))

    def configure(self, cmd_list):
        self.conf_logs.append(self.ssh_conn.send_config_set(cmd_list, strip_command=False, strip_prompt=False))

    def reset(self):
        self.connection_status = True
        self.connection_error_msg = ""
        self.cmd_logs = []
        self.conf_logs = []


class NetworkDeviceIOS(NetworkDevice):
    def __init__(self, ip, host):
        NetworkDevice.__init__(self, ip, host)
        self.os_type = "cisco_ios"

    def commit(self):
        try:
            self.conf_logs.append(self.ssh_conn.save_config())
        except Exception as err_msg:
            self.conf_logs.append(f"COMMIT is OK after msg:{err_msg}")
            self.conf_logs.append(self.ssh_conn.send_command("\n", expect_string=r"#"))


class NetworkDeviceXR(NetworkDevice):
    def __init__(self, ip, host):
        NetworkDevice.__init__(self, ip, host)
        self.os_type = "cisco_xr"

    def configure(self, cmd_list):
        self.ssh_conn.send_config_set(cmd_list)
        self.conf_logs.append(self.ssh_conn.send_command("show configuration"))

    def commit(self):
        self.conf_logs.append(self.ssh_conn.commit())
        self.ssh_conn.exit_config_mode()


class NetworkDeviceXE(NetworkDevice):
    def __init__(self, ip="", host=""):
        NetworkDevice.__init__(self, ip, host)
        self.os_type = "cisco_xe"

    def commit(self):
        self.conf_logs.append(self.ssh_conn.save_config())


class NetworkDeviceHuawei(NetworkDevice):
    def __init__(self, ip="", host=""):
        NetworkDevice.__init__(self, ip, host)
        self.os_type = "huawei"

    def commit(self):
        try:
            self.conf_logs.append(self.ssh_conn.save_config())
        except Exception as err_msg:
            self.conf_logs.append(f"COMMIT error: {err_msg}")


class NetworkDeviceMX(NetworkDevice):
    def __init__(self, ip="", host=""):
        NetworkDevice.__init__(self, ip, host)
        self.os_type = "juniper"

    def configure(self, cmd):
        self.ssh_conn.send_config_set(cmd)
        self.conf_logs.append(self.ssh_conn.send_command("show | compare"))

    def commit(self):
        self.conf_logs.append(self.ssh_conn.commit())
        self.ssh_conn.exit_config_mode()


#######################################################################################
# ------------------------------ def function part -----------------------------------#
#######################################################################################


def get_arguments(arguments):
    settings = {"conf": False, "maxth": 20, "os_type": "cisco_ios"}
    mt_pattern = re.compile(r"mt([0-9]+)")

    for arg in arguments:
        if "mt" in arg:
            match = re.search(mt_pattern, arg)
            if match and int(match.group(1)) <= 100:
                settings["maxth"] = int(match[1])
        elif arg == "cfg":
            settings["conf"] = True
        elif arg in ("xr", "xe", "hua", "mx"):
            settings["os_type"] = arg

    print()
    print(
          f"max threads:...................{settings['maxth']}\n"
          f"config mode:...................{settings['conf']}\n"
          f"OS:............................{settings['os_type']}\n"
          )
    return settings


def get_user_pw():
    with open("psw.yaml") as file:
        user_psw = yaml.load(file, yaml.SafeLoader)

    return user_psw[0], user_psw[1]


def get_device_info(yaml_file, settings):
    devices = []
    with open(yaml_file, "r") as file:
        devices_info = yaml.load(file, yaml.SafeLoader)
        if settings["os_type"] == "cisco_ios":
            for hostname, ip_address in devices_info.items():
                device = NetworkDeviceIOS(ip=ip_address, host=hostname)
                devices.append(device)
        elif settings["os_type"] == "xr":
            for hostname, ip_address in devices_info.items():
                device = NetworkDeviceXR(ip=ip_address, host=hostname)
                devices.append(device)
        elif settings["os_type"] == "xe":
            for hostname, ip_address in devices_info.items():
                device = NetworkDeviceXE(ip=ip_address, host=hostname)
                devices.append(device)
        elif settings["os_type"] == "hua":
            for hostname, ip_address in devices_info.items():
                device = NetworkDeviceHuawei(ip=ip_address, host=hostname)
                devices.append(device)
        elif settings["os_type"] == "mx":
            for hostname, ip_address in devices_info.items():
                device = NetworkDeviceMX(ip=ip_address, host=hostname)
                devices.append(device)
        else:
            print("unknown os type")

    return devices


def cmd(device, settings):
    if settings["conf"]:
        with open("cfg.yaml", "r") as file:
            yaml_input = yaml.load(file, yaml.SafeLoader)
            device.configure(yaml_input)
            device.commit()
        if "%" in "".join(device.conf_logs):
            print(f"{device.hostname:23}{device.ip_address:16}configuration ERROR %")
    else:
        with open("cmd.yaml", "r") as file:
            yaml_input = yaml.load(file, yaml.SafeLoader)
            for cmd in yaml_input:
                device.show_commands(cmd)


def write_logs(devices, current_time, log_folder, settings):
    unavailable_devices_count = 0

    conn_msg_filename = log_folder / f"{current_time}_connection_error_msg.txt"
    conn_msg_filename_file = open(conn_msg_filename, "w")

    if settings["conf"]:
        filename = log_folder / f"{current_time}_conf_logs.txt"
        with open(filename, "w") as file:
            for device in devices:
                if device.connection_status:
                    file.write("#" * 80 + "\n")
                    file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
                    file.write("".join(device.conf_logs))
                    file.write("\n\n")
                else:
                    unavailable_devices_count += 1
                    conn_msg_filename_file.write("-" * 80 + "\n")
                    conn_msg_filename_file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
                    conn_msg_filename_file.write(f"{device.connection_error_msg}\n")
                    conn_msg_filename_file.write("\n\n")

    else:
        filename = log_folder / f"{current_time}_show_commands_log.txt"
        with open(filename, "w") as file:
            for device in devices:
                if device.connection_status:
                    file.write("#" * 80 + "\n")
                    file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
                    file.write("".join(device.cmd_logs))
                    file.write("\n\n")
                else:
                    unavailable_devices_count += 1
                    conn_msg_filename_file.write("-" * 80 + "\n")
                    conn_msg_filename_file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
                    conn_msg_filename_file.write(f"{device.connection_error_msg}\n")
                    conn_msg_filename_file.write("\n\n")

    conn_msg_filename_file.close()

    if all([d.connection_status is True for d in devices]):
        conn_msg_filename.unlink()

    return unavailable_devices_count


#######################################################################################
# -----------------------------            -------------------------------------------#
#######################################################################################

def connect_dev(my_username, my_password, dev_queue, settings):
    while True:
        device = dev_queue.get()
        i = 0
        while True:
            try:
                device.ssh_conn = ConnectHandler(device_type=device.os_type, ip=device.ip_address,
                                                 username=my_username, password=my_password)
                cmd(device, settings)
                device.ssh_conn.disconnect()
                dev_queue.task_done()
                break

            except NetMikoTimeoutException as err_msg:
                device.connection_status = False
                device.connection_error_msg = str(err_msg)
                print(f"{device.hostname:23}{device.ip_address:16}timeout")
                dev_queue.task_done()
                break

            except SSHException:
                if i == 2:  # tries
                    device.connection_status = False
                    device.connection_error_msg = str(err_msg)
                    print(f"{device.hostname:23}{device.ip_address:16}BREAK SSHException occurred \t i={i}")
                    dev_queue.task_done()
                    break
                i += 1
                device.reset()
                print(f"{device.hostname:23}{device.ip_address:16}SSHException occurred \t i={i}")
                time.sleep(5)

            except Exception as err_msg:
                if i == 2:  # tries
                    device.connection_status = False
                    device.connection_error_msg = str(err_msg)
                    print(f"{device.hostname:23}{device.ip_address:16}BREAK connection failed \t i={i}")
                    dev_queue.task_done()
                    break
                else:
                    i += 1
                    device.reset()
                    print(f"{device.hostname:23}{device.ip_address:16}ERROR connection failed \t i={i}")
                    time.sleep(5)

#######################################################################################
# ------------------------------ main part -------------------------------------------#
#######################################################################################


starttime = datetime.now()
current_date = starttime.strftime("%Y.%m.%d")
current_time = starttime.strftime("%H.%M.%S")

log_folder = Path(f"{Path.cwd()}/logs/{current_date}/")  # current dir / logs / date /
log_folder.mkdir(exist_ok=True)

q = queue.Queue()

settings = get_arguments(argv)
username, password = get_user_pw()

devices = get_device_info("devices.yaml", settings)

total_devices = len(devices)

print("-------------------------------------------------------------------------------------------------------")
print("hostname               ip address      comment")
print("---------------------- --------------- ----------------------------------------------------------------")


for i in range(settings["maxth"]):
    thread = Thread(target=connect_dev, args=(username, password, q, settings))
    thread.daemon = True
    thread.start()

for device in devices:
    q.put(device)

q.join()

print()
unavailable_devices_count = write_logs(devices, current_time, log_folder, settings)
duration = datetime.now() - starttime

print("-------------------------------------------------------------------------------------------------------")
print(f"failed connection: {unavailable_devices_count} / total device number: {total_devices}")
print(f"elapsed time: {duration}")
print("-------------------------------------------------------------------------------------------------------")
