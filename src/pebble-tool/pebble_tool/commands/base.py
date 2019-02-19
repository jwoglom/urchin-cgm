__author__ = 'katharine'

from six import with_metaclass

import argparse
import logging
import os
import time

from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import QemuTransport
from libpebble2.communication.transports.websocket import WebsocketTransport
from libpebble2.communication.transports.serial import SerialTransport
from libpebble2.exceptions import ConnectionError
from libpebble2.protocol.system import TimeMessage, SetUTC

from pebble_tool.exceptions import ToolError
from pebble_tool.sdk import pebble_platforms, sdk_version
from pebble_tool.sdk.emulator import ManagedEmulatorTransport, get_all_emulator_info
from pebble_tool.sdk.cloudpebble import CloudPebbleTransport
from pebble_tool.util.analytics import post_event

_CommandRegistry = []


class SelfRegisteringCommand(type):
    def __init__(cls, name, bases, dct):
        if hasattr(cls, 'command') and cls.command is not None:
            _CommandRegistry.append(cls)
        super(SelfRegisteringCommand, cls).__init__(name, bases, dct)


class BaseCommand(with_metaclass(SelfRegisteringCommand)):
    command = None
    has_subcommands = False

    @classmethod
    def add_parser(cls, parser):
        if hasattr(cls, 'epilog'):
            epilog = cls.epilog
        elif cls.has_subcommands:
            epilog = "For help on an individual subcommand, call that command with --help."
        else:
            epilog = None
        parser = parser.add_parser(cls.command, parents=cls._shared_parser(), help=cls.__doc__, epilog=epilog,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.set_defaults(func=lambda x: cls()(x))
        return parser

    @classmethod
    def _shared_parser(cls):
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('-v', action='count', default=0, help="Degree of verbosity (use more v for more verbosity)")
        return [parser]

    def __call__(self, args):
        self._set_debugging(args.v)
        post_event("invoke_command_{}".format(self.command))

    def _set_debugging(self, level):
        self._verbosity = level
        if level is not None:
            if level == 1:
                verbosity = logging.INFO
            elif level >= 2:
                verbosity = logging.DEBUG
            else:
                verbosity = logging.WARNING
            logging.getLogger().setLevel(verbosity)


class PebbleCommand(BaseCommand):
    valid_connections = {'phone', 'qemu', 'cloudpebble', 'emulator', 'serial'}

    @classmethod
    def _shared_parser(cls):
        parser = argparse.ArgumentParser(add_help=False)
        if len(cls.valid_connections) < 2 :
            group = parser
        else :
            group = parser.add_mutually_exclusive_group()
        if 'phone' in cls.valid_connections:
            group.add_argument('--phone', metavar='phone_ip',
                                help="When using the developer connection, your phone's IP or hostname. "
                                     "Equivalent to PEBBLE_PHONE.")
        if 'qemu' in cls.valid_connections:
            group.add_argument('--qemu', nargs='?', const='localhost:12344', metavar='host',
                                help="Use this option to connect directly to a QEMU instance. "
                                     "Equivalent to PEBBLE_QEMU.")
        if 'cloudpebble' in cls.valid_connections:
            group.add_argument('--cloudpebble', action='store_true', help="Use this option to connect to your phone via"
                                                                          " the CloudPebble connection. Equivalent to "
                                                                          "PEBBLE_CLOUDPEBBLE.")
        if 'emulator' in cls.valid_connections:
            emu_group = group.add_argument_group()
            emu_group.add_argument('--emulator', type=str, help="Launch an emulator. Equivalent to PEBBLE_EMULATOR.",
                               choices=pebble_platforms)
            emu_group.add_argument('--sdk', type=str, help="SDK version to launch. Defaults to the active SDK"
                                                       " (currently {})".format(sdk_version()))
        if 'serial' in cls.valid_connections:
            group.add_argument('--serial', type=str, help="Connected directly, given a path to a serial device.")
        return super(PebbleCommand, cls)._shared_parser() + [parser]

    def __call__(self, args):
        super(PebbleCommand, self).__call__(args)
        try:
            self.pebble = self._connect(args)
        except ConnectionError as e:
            raise ToolError(str(e))

    def _connect(self, args):
        self._set_debugging(args.v)
        if getattr(args, 'phone', None):
            return self._connect_phone(args.phone)
        elif getattr(args, 'qemu', None):
            return self._connect_qemu(args.qemu)
        elif getattr(args, 'emulator', None):
            return self._connect_emulator(args.emulator, args.sdk)
        elif getattr(args, 'cloudpebble', None):
            return self._connect_cloudpebble()
        elif getattr(args, 'serial', None):
            return self._connect_serial(args.serial)
        else:
            if 'phone' in self.valid_connections and 'PEBBLE_PHONE' in os.environ:
                return self._connect_phone(os.environ['PEBBLE_PHONE'])
            elif 'qemu' in self.valid_connections and 'PEBBLE_QEMU' in os.environ:
                return self._connect_qemu(os.environ['PEBBLE_QEMU'])
            elif 'cloudpebble' in self.valid_connections and os.environ.get('PEBBLE_CLOUDPEBBLE', False):
                return self._connect_cloudpebble()
            elif 'serial' in self.valid_connections and 'PEBBLE_BT_SERIAL' in os.environ:
                return self._connect_serial(os.environ['PEBBLE_BT_SERIAL'])
            elif 'emulator' in self.valid_connections:
                running = []
                emulator_platform = None
                emulator_sdk = None
                if 'PEBBLE_EMULATOR' in os.environ:
                    emulator_platform = os.environ['PEBBLE_EMULATOR']
                    if emulator_platform not in pebble_platforms:
                        raise ToolError("PEBBLE_EMULATOR is set to '{}', which is not a valid platform "
                                        "(pick from {})".format(emulator_platform, ', '.join(pebble_platforms)))
                    emulator_sdk = os.environ.get('PEBBLE_EMULATOR_VERSION', sdk_version())
                else:
                    for platform, sdks in get_all_emulator_info().items():
                        for sdk in sdks:
                            if ManagedEmulatorTransport.is_emulator_alive(platform, sdk):
                                running.append((platform, sdk))
                    if len(running) == 1:
                        emulator_platform, emulator_sdk = running[0]
                    elif len(running) > 1:
                        raise ToolError("Multiple emulators are running; you must specify which to use.")
                if emulator_platform is not None:
                    return self._connect_emulator(emulator_platform, emulator_sdk)
        raise ToolError("No pebble connection specified.")

    def _connect_phone(self, phone):
        parts = phone.split(':')
        ip = parts[0]
        if len(parts) == 2:
            port = int(parts[1])
        else:
            port = 9000
        connection = PebbleConnection(WebsocketTransport("ws://{}:{}/".format(ip, port)), **self._get_debug_args())
        connection.connect()
        connection.run_async()
        return connection

    def _connect_qemu(self, qemu):
        parts = qemu.split(':')
        ip = parts[0]
        if not ip:
            ip = '127.0.0.1'
        if len(parts) == 2:
            port = int(parts[1])
        else:
            port = 12344
        connection = PebbleConnection(QemuTransport(ip, port), **self._get_debug_args())
        connection.connect()
        connection.run_async()
        return connection

    def _connect_emulator(self, platform, sdk):
        connection = PebbleConnection(ManagedEmulatorTransport(platform, sdk), **self._get_debug_args())
        connection.connect()
        connection.run_async()
        # Make sure the timezone is set usefully.
        if connection.firmware_version.major >= 3:
            ts = time.time()
            tz_offset = -time.altzone if time.localtime(ts).tm_isdst and time.daylight else -time.timezone
            tz_offset_minutes = tz_offset // 60
            tz_name = "UTC%+d" % (tz_offset_minutes / 60)
            connection.send_packet(TimeMessage(message=SetUTC(unix_time=ts, utc_offset=tz_offset_minutes, tz_name=tz_name)))
        return connection

    def _connect_cloudpebble(self):
        connection = PebbleConnection(CloudPebbleTransport(), **self._get_debug_args())
        connection.connect()
        connection.run_async()
        return connection

    def _connect_serial(self, device):
        connection = PebbleConnection(SerialTransport(device), **self._get_debug_args())
        connection.connect()
        connection.run_async()
        return connection

    def _get_debug_args(self):
        args = {}
        if self._verbosity >= 3:
            args['log_packet_level'] = logging.DEBUG
        if self._verbosity >= 4:
            args['log_protocol_level'] = logging.DEBUG
        return args


def register_children(parser):
    subparsers = parser.add_subparsers(title="command")
    for command in _CommandRegistry:
        command.add_parser(subparsers)
