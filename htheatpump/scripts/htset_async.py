#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#  htheatpump - Serial communication module for Heliotherm heat pumps
#  Copyright (C) 2023  Daniel Strigl

#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

""" Command line tool to set the value of a specific parameter of the heat pump.

    Example:

    .. code-block:: shell

       $ python3 htset_async.py --device /dev/ttyUSB1 "HKR Soll_Raum" "21.5"
       or
       $ python3 htset_async.py --url "tcp://localhost:9999" "HKR Soll_Raum" "21.5"
       21.5
"""

import argparse
import asyncio
import logging
import sys
import textwrap
from typing import Final

from htheatpump.aiohtheatpump import AioHtHeatpump
from htheatpump.htparams import HtParams
from htheatpump.utils import Timer

_LOGGER: Final = logging.getLogger(__name__)


class ParamNameAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # type:ignore
        for name in values:
            if name not in HtParams:
                raise ValueError("Unknown parameter {!r}".format(name))
        setattr(namespace, self.dest, values)


# Main program
async def main_async() -> None:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(
            """\
            Command line tool to set the value of a specific parameter of the heat pump.

            Example:

              $ python3 htset_async.py --device /dev/ttyUSB1 "HKR Soll_Raum" "21.5"
              or
              $ python3 htset.py --url "tcp://localhost:9999" "HKR Soll_Raum" "21.5"
              21.5
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            DISCLAIMER
            ----------

              Please note that any incorrect or careless usage of this program as well as
              errors in the implementation can damage your heat pump!

              Therefore, the author does not provide any guarantee or warranty concerning
              to correctness, functionality or performance and does not accept any liability
              for damage caused by this program or mentioned information.

              Thus, use it on your own risk!
            """
        )
        + "\r\n",
    )

    parser.add_argument(
        "-u",
        "--url",
        type=str,
        help="the (TCP socket) url on which the heat pump is connected",
    )

    parser.add_argument(
        "-d",
        "--device",
        default="/dev/ttyUSB0",
        type=str,
        help="the serial device on which the heat pump is connected, default: %(default)s",
    )

    parser.add_argument(
        "-b",
        "--baudrate",
        default=115200,
        type=int,
        # the supported baudrates of the Heliotherm heat pump (HP08S10W-WEB):
        choices=[9600, 19200, 38400, 57600, 115200],
        help="baudrate of the serial connection (same as configured on the heat pump), default: %(default)s",
    )

    parser.add_argument(
        "-t", "--time", action="store_true", help="measure the execution time"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="increase output verbosity by activating logging",
    )

    parser.add_argument(
        "name",
        type=str,
        nargs=1,
        action=ParamNameAction,
        help="parameter name (as defined in htparams.csv)",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        # Use the default timeout defined in the HtHeatpump class
        default=AioHtHeatpump.DEFAULT_TIMEOUT,
        help="connection timeout in seconds, default: %(default)s",
    )

    parser.add_argument("value", type=str, nargs=1, help="parameter value (as string)")

    args = parser.parse_args()

    # activate logging with level DEBUG in verbose mode
    log_format = "%(asctime)s %(levelname)s [%(name)s|%(funcName)s]: %(message)s"
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format=log_format)
    else:
        logging.basicConfig(level=logging.WARNING, format=log_format)

    try:
        if (args.url):
            # Use keyword argument 'url'
            hp = AioHtHeatpump(url=args.url, timeout=args.timeout)
            if args.verbose:
                _LOGGER.info("--url specified, using url-based connection: %s", args.url)
        else:
            # Use keyword argument 'device' and pass serial-specific options
            hp = AioHtHeatpump(device=args.device, baudrate=args.baudrate, timeout=args.timeout)
            if args.verbose:
                _LOGGER.info("--device specified, using serial connection: %s", args.device)

        hp.open_connection()
        await hp.login_async()

        rid = await hp.get_serial_number_async()
        if args.verbose:
            _LOGGER.info(
                "connected successfully to heat pump with serial number %d", rid
            )
        ver = await hp.get_version_async()
        if args.verbose:
            _LOGGER.info("software version = %s (%d)", *ver)

        # convert the passed value (as string) to the specific data type
        value = HtParams[args.name[0]].from_str(args.value[0])
        # set the parameter of the heat pump to the passed value
        with Timer() as timer:
            value = await hp.set_param_async(args.name[0], value, True)
        exec_time = timer.elapsed
        print(value)

        # print execution time only if desired
        if args.time:
            print("execution time: {:.2f} sec".format(exec_time))

    except Exception as ex:
        _LOGGER.exception(ex)
        sys.exit(1)
    finally:
        await hp.logout_async()  # try to logout for an ordinary cancellation (if possible)
        await hp.close_connection_async()

    sys.exit(0)


def main() -> None:
    # run the async main application
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
