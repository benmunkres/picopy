from math import floor
import datetime

import configparser

from gpiozero import LED, Button
from time import sleep, time
import os
from glob import glob
from shutil import disk_usage
import shlex
import subprocess
import threading
from pathlib import Path
import queue

print(f"started picopy at {datetime.datetime.now()}")

############################ PiCopy Parameters ############################
### import config file:
# default paths: /home/pi/picopy/picopy.conf, /etc/picopy.conf
PICOPY_CONF_PATHS = ["~/picopy/picopy.conf", "/etc/picopy.conf"]

# attempt to load config
conf_loaded = False
for p in PICOPY_CONF_PATHS:
    p = Path(p).expanduser()
    if not p.exists():
        continue

    # load the config file
    parser = configparser.ConfigParser()
    parser.read(p)

    print(parser.sections())

    try:
        ### Storage:
        # interval between checks for newly plugged-in drives (in seconds)
        MOUNT_CHECK_INTERVAL = int(parser['STORAGE']['MOUNT_CHECK_INTERVAL'])

        # location of mounted drives (should be left as default for a typical rPi)
        MOUNT_LOCATION = parser['STORAGE']['MOUNT_LOCATION']

        # file/folder to look for to identify the destination
        COPY_DESTINATION_ID = parser['STORAGE']['COPY_DESTINATION_ID']

        ### File Copying Related:
        # file extentions of 'target' files
        # note that this will match extentions that are all lowercase or all capitals (but not weird combinations)
        TARGET_FILE_EXTENTIONS = parser['FILE_COPYING']['TARGET_FILE_EXTENTIONS'].split(",")

        # files and/or folders to ignore while copying
        EXCLUDE_FILES = parser['FILE_COPYING']['EXCLUDE_FILES'].split(",")

        # minimum file size for target files
        MIN_FILE_SIZE = parser['FILE_COPYING']['MIN_FILE_SIZE']
    except:
        print(f"Error Reading Config File {p}, reverting to defaults")
        continue

    conf_loaded = True

## if all paths led to invalid config files
if not conf_loaded:
    print(f"reverting to hardcoded parameter defaults")
    MOUNT_CHECK_INTERVAL = 1
    MOUNT_LOCATION = "/media/pi"
    COPY_DESTINATION_ID = "PICOPY_DESTINATION"
    TARGET_FILE_EXTENTIONS = [".wav"]
    EXCLUDE_FILES = ['.Trashes', '.fsevents*', 'System*', '.Spotlight*']
    MIN_FILE_SIZE = "100k" # 100Kb

# add all caps/lower case versions of extentions
TARGET_FILE_EXTENTIONS = [f"*{ext.lower()}" for ext in
                          TARGET_FILE_EXTENTIONS] + [f"*{ext.upper()}" for ext in
                                                     TARGET_FILE_EXTENTIONS]

### System:
UI_SLEEP_TIME = 0.1 # sleep time between main loop iterations, in seconds

############################ Utility Functions ############################
def log(msg):
    """Print a Log Message with the current time and state"""
    print(f"{datetime.datetime.now()} [{state}]:\t{msg}")


def output_parser(process):
    """read output from Popen STDOUT"""
    out = []
    for line in iter(process.stdout.readline, b""):
        out.append(line.decode("utf-8"))
    return out


def output_reader(process, outq):
    """send output from Popen STDOUT to a queue"""
    for line in iter(process.stdout.readline, b""):
        outq.put(line.decode("utf-8"))


def get_free_space(disk, scale=2**30):
    """Get the free space on a disk"""
    return float(disk_usage(disk).free) / scale


def get_used_space(disk, scale=2**30):
    """Get the space used on a disk"""
    return float(disk_usage(disk).used) / scale


def blink_error(n, reps=2):
    """blink the error led to send a message"""
    global error_led
    error_led.blink(0.2, 0.2, n=reps, background=False)

def get_src_drive():  # TODO: blink the drive LED rather than error
    """search for source and destination drives mounted at mount_location
    a source drive does is any drive listed in /media/pi/ that does not have a file/folder named PICOPY_DESTINATION in root directory
    must find exactly one. if zero returns None, if >1 blinks error"""
    drives = glob(f"{MOUNT_LOCATION}/*")
    src_drives = []
    for d in drives:
        if not os.path.exists(f"{d}/{COPY_DESTINATION_ID}"):
            src_drives.append(d)
    if len(src_drives) > 1:
        log("ERR: found multiple source drives")
        blink_error(3, 2)
        return None
    elif len(src_drives) < 1:
        return None
    return src_drives[0]


def get_dest_drive():
    # a destination drive has file/folder {COPY_DESTINATION_ID} in root directory
    # must find exactly one. if zero returns None, if >1 blinks error
    drives = glob(f"{MOUNT_LOCATION}/*")
    dest_drives = []
    for d in drives:
        if os.path.exists(f"{d}/{COPY_DESTINATION_ID}"):
            dest_drives.append(d)
    if len(dest_drives) > 1:
        log("ERR: found multiple destination drives")
        blink_error(4, 2)
        return None
    elif len(dest_drives) < 1:
        return None
    return dest_drives[0]


def eject_drive(drive):
    """Eject the given drive"""
    log(f"attempting to eject drive {drive}")

    if drive is None:
        log("ERR: no drive to eject")
    else:
        # try to eject (unmount) the disk with system umount command
        cmd = f"umount '{drive}'"
        log(cmd)
        # Start the process
        process = subprocess.Popen(
            shlex.split(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True  # Automatically decodes bytes to strings
        )
        # Safely capture full output and wait for process to finish
        output, _ = process.communicate()
        exit_code = process.returncode

        # Parse the captured string output
        if output:
            for line in output.splitlines():
                log(line)

        # Check final status
        if exit_code == 0:
            log(f"ejected {drive}")
        else:
            log(f"ERR: failed to eject {drive}")
    sleep(0.1)

def prepare_copy(source, dest):
    """
    Check to see if the system is ready for a copy;
     - are drives available and mounted?
     - is there enough free space on the drives?
    """
    log("checking for source and dest drives")

    if source is None:
        blink_error(3, 3)
        log("ERR: no source drive found")
        return False

    if dest is None:
        blink_error(4, 3)
        log(
            f"ERR: no destination drive found. Dest should contain file or folder {COPY_DESTINATION_ID} in root"
        )
        return False

    log(f"found source drive {source} and destination drive {dest}")

    # ok, now we know we have 1 source and 1 destination
    # check that enough space on the dest for source
    log("checking free space")
    try:
        src_size = get_used_space(source)
    except OSError:
        log("ERR: I/O error, card likely corrupted. Please copy manually!")
        blink_error(6, 3)
        return False
    dest_free = get_free_space(dest)
    log(f"\tsrc size: {src_size} Gb")
    log(f"\tdest free: {dest_free} Gb")
    if src_size > dest_free:
        log("ERR: not enough space on dest for source")
        blink_error(5, 2)  # raise NotEnoughSpaceError
        return False

    # if we make it to here, we are ready to copy
    # there is a source and a destination with enough space for it
    return True

def progress_monitor(progress_queue, progress_led):
    """
    Blink Progress LED based on transfer progress
    """
    progress_frac = 0
    while True:
        # try to update the progress fraction
        while not progress_queue.empty():
            try:
                progress_frac = progress_queue.get_nowait()
            except queue.Empty:
                break

        if progress_frac is None:
            return

        progress_outoften = floor(progress_frac*10)

        # blink LED progress outof10 times
        progress_led.blink(0.1, 0.15, progress_outoften, background=False)
        sleep(3 - 0.25 * progress_outoften)

def start_copy_thread(source, dest):
    """
    Start the rsync process to copy the data
    """
    log("copying")
    sleep(0.5)
    time_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_save_dir = dest + "/" + os.path.basename(source) + "_" + time_str

    # first create the directory
    Path(dest_save_dir).mkdir(exist_ok=True, parents=True)

    # we will run two rsync commands, copying all non-wav files then including wav files over min_file_size
    # first copy everything except .wav, .WAV, and architve files we don't want
    exclude_flags = "".join([f"--exclude '{f}' " for f in EXCLUDE_FILES])
    cmd = (
        f"rsync -rvt --log-file=./rsync.log --progress --max-size={MIN_FILE_SIZE} "
        + exclude_flags
        + "".join([f"--exclude '{f}' " for f in TARGET_FILE_EXTENTIONS])
        + f"'{source}' '{dest_save_dir}'"
    )

    log(cmd)
    subprocess.run(shlex.split(cmd))

    # second, copy .wav and .WAV files above min_file_size
    cmd = (
        f"rsync -rvt --log-file=./rsync.log --min-size={MIN_FILE_SIZE} --progress "
        + "--include '*/' "
        + "".join([f"--include '{f}' " for f in TARGET_FILE_EXTENTIONS])
        + "--exclude '*' "
        + f"'{source}' '{dest_save_dir}'"
    )

    log(cmd)
    rsync_process = subprocess.Popen(
        shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    # start a thread to watch the rsync process and catch output
    rsync_outq = queue.Queue()

    rsync_thread = threading.Thread(
        target=output_reader, args=(rsync_process, rsync_outq)
    )
    rsync_thread.start()

    # return the queue, thread, and process
    # we can read the queue and terminate the process from outside this function
    return (rsync_process, rsync_outq, rsync_thread, dest_save_dir)


def check_dest_synced(source, dest, dest_save_dir):
    log("checking if dest has all files from source")
    start_time = time()

    n_files_out_of_sync = 0

    # check sync of non wav/WAV files: (dry run with -n flag and --stats)
    exclude_flags = "".join([f"--exclude '{f}' " for f in EXCLUDE_FILES])
    cmd = (
        f"rsync -rvn --stats --progress --size-only --max-size={MIN_FILE_SIZE} "
        + exclude_flags
        + "".join([f"--exclude '{f}' " for f in TARGET_FILE_EXTENTIONS])
        + f"'{source}' '{dest_save_dir}'"
    )
    log(cmd)
    check_process = subprocess.Popen(
        shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    return_values = [
        f
        for f in output_parser(check_process)
        if "Number of regular files transferred" in f
    ]
    log(return_values)
    n_files_out_of_sync += int(return_values[0].split(" ")[-1])

    # check sync of all wav/WAV files over size limit:
    # rsync command (dry run) to see if any files would be transferred based on size difference
    cmd = (
        f"rsync -rvn --stats --min-size={MIN_FILE_SIZE} --progress --size-only "
        + "--include '*/' "
        + "".join([f"--include '{f}' " for f in TARGET_FILE_EXTENTIONS])
        + "--exclude '*' "
        + f"'{source}' '{dest_save_dir}'"
    )
    log(cmd)
    check_process = subprocess.Popen(
        shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    # check_process.communicate()
    return_values = [
        f
        for f in output_parser(check_process)
        if "Number of regular files transferred" in f
    ]
    log(return_values)

    n_files_out_of_sync += int(return_values[0].split(" ")[-1])
    log(n_files_out_of_sync)
    return n_files_out_of_sync == 0


############################ MAIN LOOP ############################
# GPIO pin setup for LEDs and Buttons
try:
    status_led = LED(18)
    progress_led = LED(27)
    error_led = LED(22)
    src_mounted_led = LED(23)
    dest_mounted_led = LED(24)
except Exception as e:
    raise Exception("""
    GPIO pins not available. Is PiCopy already running? Try stopping it
    via `sudo systemctl stop picopy.service` or by finding the pycopy
    process in htop.
    """) from e

run_button = Button(4, hold_time=1)
stop_button = Button(17, hold_time=1)
eject_button = Button(5, hold_time=1)
# power button is GPIO3, but managed by a separate script

# initialize global variables
rsync_process = None
rsync_outq = None
rsync_thread = None
dest_save_dir = None

#### Main Loop:
states = {"IDLE", "READY_COPY", "COPYING", "CHECK_COPY", "COMPLETE_TRANSFER", "INCOMPLETE_TRANSFER"}

### Initial State
state = "IDLE"

state_leds_updated = False

source_drive = None
dest_drive = None

# time of the last mount check
last_mount_check = time()

# copy progress
copy_progress_queue = None
copy_progress_thread = None

### Setup Button Callbacks
# button state callback variables
run_button_pressed = False
stop_button_pressed = False
eject_button_pressed = False

### button press callbacks
def run_pressed_event():
    global run_button_pressed
    run_button_pressed = True

def stop_pressed_event():
    global stop_button_pressed
    stop_button_pressed = True

def eject_pressed_event():
    global eject_button_pressed
    eject_button_pressed = True

run_button.when_pressed = run_pressed_event
stop_button.when_pressed = stop_pressed_event
eject_button.when_pressed = eject_pressed_event

while True:
    sleep(UI_SLEEP_TIME)

    ### LED Update Logic
    if not state_leds_updated:
        match state:
            case "IDLE":
                error_led.off()
                progress_led.off()
                status_led.blink(0.1, 2.9, n=None, background=True)
            case "READY_COPY":
                status_led.blink(1, 1, n=None, background=True)
            case "COPYING":
                status_led.blink(0.25, 0.25, n=None, background=True)
            case "CHECK_COPY":
                progress_led.on()
                status_led.blink(0.25, 0.25, n=None, background=True)
            case "COMPLETE_TRANSFER":
                progress_led.on()
                status_led.on()
            case "INCOMPLETE_TRANSFER":
                progress_led.off()
                status_led.off()
                error_led.on()

        state_leds_updated = True

    ### I/O Status Flags
    # copy ready state
    copy_ready = False

    # copying state:
    copy_done = False
    copy_succeeded = False

    # copy checking state
    successful_check = False

    ### I/O Logic:
    match state:
        case "IDLE":
            ## check for newly mounted drives, if needed
            if (time() - last_mount_check) >= MOUNT_CHECK_INTERVAL:
                source_drive = get_src_drive()
                dest_drive = get_dest_drive()
                src_mounted_led.off() if source_drive is None else src_mounted_led.on()
                dest_mounted_led.off() if dest_drive is None else dest_mounted_led.on()

            ## if eject is pressed, eject the disks
            if eject_button_pressed:
                # eject the source drive if it's mounted, otherwise eject the destination
                sel_drive = source_drive if (source_drive is not None) else dest_drive
                eject_drive(drive = sel_drive)
                eject_button_pressed = False

            ## if RUN is pressed, check if we're ready to copy
            if run_button_pressed:
                copy_ready = prepare_copy(source=source_drive, dest=dest_drive)

            copy_progress = 0

        case "READY_COPY":
            ## Start the copy operation when run is pressed
            if run_button_pressed and (not stop_button_pressed):
                # if run is pressed, initiate the copy operation
                rsync_process, rsync_outq, rsync_thread, dest_save_dir = (
                    start_copy_thread(source_drive, dest_drive)
                )

                # start thread to blink progress light
                progress_queue = queue.Queue()
                progress_monitor_thread = threading.Thread(
                    target=progress_monitor, args=(progress_queue, progress_led)
                )
                progress_monitor_thread.start()

        case "COPYING":
            ## Check to see if the copying operation is done:
            if not rsync_thread.is_alive():
                log("rsync thread finished")
                copy_done = True

                try:
                    rsync_process.wait(5) # wait for up to five seconds for process to finish
                except:
                    log("rsync process didn't terminate properly after 5 seconds")

                return_code = rsync_process.returncode
                copy_succeeded = (return_code is not None) and (return_code == 0)
                if not copy_succeeded:
                    log(f"rsync process failed, exiting with code {return_code}")

                progress_queue.put(None)
                progress_monitor_thread.join()

            ## Copy operation is canceled
            elif stop_button_pressed:
                copy_done = True
                copy_succeeded = False
                ## cancel the copying operation
                # if status is copying and rsync process is running, can cel it
                log("canceling copy")
                rsync_process.terminate()
                try:
                    rsync_process.wait(timeout=5)
                    log(f"== subprocess rsync_process exited with rx={rsync_process.returncode}")
                except subprocess.TimeoutExpired:
                    log("subprocess rsync_process did not terminate in time")

                progress_queue.put(None)
                progress_monitor_thread.join()


            ## otherwise prepare for next copy state
            # read lines from rsync output
            line = None
            xfer_line = None
            while True: # this is janky as shit
                try:
                    line = rsync_outq.get(block=False)
                    print(line)
                    if "to-chk=" in line: # check if line has the number of files left to check
                        xfer_line = line
                except queue.Empty:
                    break  # no lines in queue

            # update status LED using messages from progress_q
            # new blinking paradigm is a four second blink with blink length being determined by
            # the fraction of the number of files transfered
            if xfer_line is not None:
                left_files, total_files = xfer_line.split("to-chk=")[-1][:-2].split("/")
                left_files, total_files = int(left_files), int(total_files)
                copy_progress = 1 - left_files/total_files
                progress_queue.put(copy_progress)

        case "CHECK_COPY":
            ## check the integrity of the copy
            successful_check = check_dest_synced(source_drive, dest_drive, dest_save_dir)
            if (successful_check):
                log("complete successful transfer, press RUN to acknowledge")
            else:
                log("ERR: incomplete transfer, press RUN to acknowledge")

        case _:
            pass


    ### State Update Logic
    nextstate = None
    match state:
        case "IDLE":
            # if run is pressed and copy_ready is true, then we can go to READY_COPY state, otherwise IDLE
            nextstate = "READY_COPY" if (run_button_pressed and copy_ready) else "IDLE"

        case "READY_COPY":
            # go to COPYING if the run button is pressed and the stop button isn't
            if stop_button_pressed:
                nextstate = "IDLE"
            elif run_button_pressed:
                nextstate = "COPYING"
            else:
                nextstate = "READY_COPY"

        case "COPYING":
            if copy_done:
                nextstate = "CHECK_COPY" if copy_succeeded else "INCOMPLETE_TRANSFER"
            else:
                nextstate = "COPYING"

        case "CHECK_COPY":
            nextstate = "COMPLETE_TRANSFER" if successful_check else "INCOMPLETE_TRANSFER"

        case "COMPLETE_TRANSFER":
            nextstate = "IDLE" if run_button_pressed else "COMPLETE_TRANSFER"

        case "INCOMPLETE_TRANSFER":
            nextstate = "IDLE" if run_button_pressed else "INCOMPLETE_TRANSFER"

        case _:
            log("ERR: Invalid State Reached")
            nextstate = "IDLE"


    # update the state:
    if nextstate != state:
        log(f"state = {state}, nextstate = {nextstate}")
        state_leds_updated = False

    if nextstate in states:
        state = nextstate
    else:
        log(f"ERR: Invalid state {nextstate} reached, reverting to IDLE")
        state = "IDLE"

    ### reset button states
    run_button_pressed = False
    stop_button_pressed = False
    eject_button_pressed = False
