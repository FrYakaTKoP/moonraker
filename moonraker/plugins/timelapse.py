# Timelapse plugin
#
# Copyright (C) 2020 Christoph Frei <fryakatkop@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import os
import glob
import re
from datetime import datetime
from tornado.ioloop import IOLoop


class Timelapse:

    def __init__(self, config):
        # setup vars
        self.renderisrunning = False
        self.takingframe = False
        self.framecount = 0
        self.lastframefile = ""
        self.lastrenderprogress = 0

        # get config
        self.enabled = config.getboolean("enabled", True)
        self.crf = config.getint("constant_rate_factor", 23)
        self.framerate = config.getint("output_framerate", 30)
        self.timeformatcode = config.get("time_format_code", "%Y%m%d_%H%M")
        self.snapshoturl = config.get(
            "snapshoturl", "http://localhost:8080/?action=snapshot")
        self.pixelformat = config.get("pixelformat", "yuv420p")
        self.extraoutputparams = config.get("extraoutputparams", "")
        out_dir_cfg = config.get("output_path", "~/timelapse/")
        temp_dir_cfg = config.get("frame_path", "/tmp/timelapse/")

        # setup directories
        out_dir_cfg = os.path.join(out_dir_cfg, '') # make sure there is a trailing "/"
        temp_dir_cfg = os.path.join(temp_dir_cfg, '')
        self.out_dir = os.path.expanduser(out_dir_cfg) # evaluate and expand "~"
        self.temp_dir = os.path.expanduser(temp_dir_cfg)
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        # setup eventhandlers and endpoints
        self.server = config.get_server()
        file_manager = self.server.lookup_plugin("file_manager")
        file_manager.register_directory("timelapse", self.out_dir)
        file_manager.register_directory("timelapse_frames", self.temp_dir)
        self.server.register_notification("timelapse:timelapse_event")
        self.server.register_event_handler(
            "server:gcode_response", self.handle_status_update)
        self.server.register_remote_method(
            "timelapse_newframe", self.call_timelapse_newframe)
        self.server.register_endpoint(
            "/machine/timelapse/render", ['POST'], self.timelapse_render)
        self.server.register_endpoint(
            "/machine/timelapse/settings", ['GET', 'POST'],
            self.webrequest_timelapse_settings)
        self.server.register_endpoint(
            "/machine/timelapse/lastframeinfo", ['GET'],
            self.webrequest_timelapse_lastframeinfo)

    async def webrequest_timelapse_lastframeinfo(self, webrequest):
        return {
            'framecount': self.framecount,
            'lastframefile': self.lastframefile
        }

    async def webrequest_timelapse_settings(self, webrequest):
        action = webrequest.get_action()
        if action == 'POST':
            args = webrequest.get_args()
            logging.debug("webreq_args: " + str(args))
            for arg in args:
                val = args.get(arg)
                if arg == "enabled":
                    self.enabled = webrequest.get_boolean(arg)
                if arg == "constant_rate_factor":
                    self.crf = webrequest.get_int(arg)
                if arg == "output_framerate":
                    self.framerate = webrequest.get_int(arg)
                if arg == "pixelformat":
                    self.pixelformat = webrequest.get(arg)
                if arg == "extraoutputparams":
                    self.extraoutputparams = webrequest.get(arg)
        return {
            'enabled': self.enabled,
            'constant_rate_factor': self.crf,
            'output_framerate': self.framerate,
            'pixelformat': self.pixelformat,
            'extraoutputparams': self.extraoutputparams
        }

    def call_timelapse_newframe(self):
        if self.enabled:
            ioloop = IOLoop.current()
            ioloop.spawn_callback(self.timelapse_newframe)
        else:
            logging.debug("NEW_FRAME macro ignored timelapse is disabled")

    async def timelapse_newframe(self):
        if not self.takingframe:
            self.takingframe = True
            self.framecount += 1
            framefile = "frame" + str(self.framecount).zfill(6) + ".jpg"
            cmd = "wget " + self.snapshoturl + " -O " \
                  + self.temp_dir + framefile
            self.lastframefile = framefile
            logging.debug(f"cmd: {cmd}")

            shell_command = self.server.lookup_plugin('shell_command')
            scmd = shell_command.build_shell_command(cmd, None)
            try:
                cmdstatus = await scmd.run(timeout=2., verbose=False)
            except Exception:
                logging.exception(f"Error running cmd '{cmd}'")

            result = {'action': 'newframe'}
            if cmdstatus:
                result.update({
                    'frame': self.framecount,
                    'framefile': framefile,
                    'status': 'success'
                })
            else:
                logging.info(f"getting newframe failed: {cmd}")
                self.framecount -= 1
                result.update({'status': 'error'})

            self.notify_timelapse_event(result)
            self.takingframe = False

    async def webrequest_timelapse_render(self, webrequest):
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.timelapse_render)
        return "ok"

    def handle_status_update(self, status):
        if status == "File selected":
            # print_started
            self.timelapse_cleanup()
        elif status == "Done printing file":
            # print_done
            if self.enabled:
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.timelapse_render)

    def timelapse_cleanup(self):
        logging.debug("timelapse_cleanup")
        filelist = glob.glob(self.temp_dir + "frame*.jpg")
        if filelist:
            for filepath in filelist:
                os.remove(filepath)
        self.framecount = 0
        self.lastframefile = ""

    async def timelapse_render(self, webrequest=None):
        filelist = glob.glob(self.temp_dir + "frame*.jpg")
        self.framecount = len(filelist)
        result = {'action': 'render'}
        if not filelist:
            msg = "no frames to render skipping"
            status = "skipped"
            cmd = outfile = None
        elif self.renderisrunning:
            msg = "render is already running"
            status = "alreadyrunning"
            cmd = outfile = None
        else:
            self.renderisrunning = True

            # get  printed filename
            klippy_apis = self.server.lookup_plugin("klippy_apis")
            kresult = await klippy_apis.query_objects({'print_stats': None})
            pstats = kresult.get("print_stats", {})
            gcodefile = pstats.get("filename", "")  # .split(".", 1)[0]

            # build shell command
            now = datetime.now()
            date_time = now.strftime(self.timeformatcode)
            inputfiles = self.temp_dir + "frame%6d.jpg"
            outsuffix = ".mp4"
            outfile = "timelapse_" + gcodefile + "_" + date_time + outsuffix
            cmd = "ffmpeg" \
                  + " -r " + str(self.framerate) \
                  + " -i '" + inputfiles + "'" \
                  + " -crf " + str(self.crf) \
                  + " -vcodec libx264" \
                  + " -pix_fmt " + self.pixelformat \
                  + " -an" \
                  + " " + self.extraoutputparams \
                  + " '" + self.out_dir + outfile + "' -y"

            # log and notify ws
            logging.debug(f"start FFMPEG: {cmd}")
            result.update({
                'status': 'started',
                'framecount': self.framecount,
                'settings': {
                    'framerate': self.framerate,
                    'crf': self.crf,
                    'pixelformat': self.pixelformat
                }
            })
            self.notify_timelapse_event(result)

            # run the command
            shell_command = self.server.lookup_plugin("shell_command")
            scmd = shell_command.build_shell_command(cmd, self.ffmpeg_response)
            try:
                cmdstatus = await scmd.run(timeout=None, verbose=True)
            except Exception:
                logging.exception(f"Error running cmd '{cmd}'")

            # check success
            if cmdstatus:
                status = "success"
                msg = f"Rendering Video successful: {outfile}"
                result.update({
                    'filename': outfile
                })
                result.pop("framecount")
                result.pop("settings")
            else:
                status = "error"
                msg = f"Rendering Video failed"

            self.renderisrunning = False

        # log and notify ws
        logging.debug(msg)
        result.update({
            'status': status,
            'msg': msg
        })
        self.notify_timelapse_event(result)

        return result

    def ffmpeg_response(self, response):
        # logging.debug(f"ffmpegResponse: {response}")
        lastcmdreponse = response.decode("utf-8")
        try:
            frame = re.search(
                '(?<=frame=)*(\d+)(?=.+fps)',
                self.lastcmdreponse
           	).group()
        except AttributeError:
            return
        percent = int(frame) / self.framecount * 100

        if self.lastrenderprogress != int(percent):
            self.lastrenderprogress = int(percent)
            # logging.debug(f"ffmpeg Progress: {self.lastrenderprogress}% ")
            result = {
                'action': 'render',
                'status': 'running',
                'progress': self.lastrenderprogress
            }
            self.notify_timelapse_event(result)

    def notify_timelapse_event(self, result):
        logging.debug(f"notify_timelapse_event: {result}")
        self.server.send_event("timelapse:timelapse_event", result)


def load_plugin(config):
    return Timelapse(config)
