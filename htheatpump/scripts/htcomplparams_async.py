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

""" Command line tool to create a complete list of all Heliotherm heat pump parameters.

    Example:

    .. code-block:: shell

       $ python3 htcomplparams_async.py --device /dev/ttyUSB1 --baudrate 9600 --csv
       or
       $ python3 htcomplparams_async.py --url "tcp://localhost:9999" --csv
       connected successfully to heat pump with serial number 123456
       software version = 3.0.20 (273)
       'SP,NR=0' [Language]: VAL=0, MIN=0, MAX=4 (dtype=INT)
       'SP,NR=1' [TBF_BIT]: VAL=0, MIN=0, MAX=1 (dtype=BOOL)
       'SP,NR=2' [Rueckruferlaubnis]: VAL=1, MIN=0, MAX=1 (dtype=BOOL)
       ...
       write data to: /home/pi/prog/htheatpump/htparams-123456-3_0_20-273.csv
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
import textwrap
from typing import Any, Dict, Final

from htheatpump.aiohtheatpump import AioHtHeatpump
from htheatpump.htparams import HtDataTypes, HtParam
from htheatpump.utils import Timer

_LOGGER: Final = logging.getLogger(__name__)


# Main program
async def main_async() -> None:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(
            """\
            Command line tool to create a complete list of all Heliotherm heat pump parameters.

            Example:

              $ python3 htcomplparams_async.py --device /dev/ttyUSB1 --baudrate 9600 --csv
              or
              $ python3 htcomplparams_async.py --url "tcp://localhost:9999" --csv
              connected successfully to heat pump with serial number 123456
              software version = 3.0.20 (273)
              'SP,NR=0' [Language]: VAL=0, MIN=0, MAX=4 (dtype=INT)
              'SP,NR=1' [TBF_BIT]: VAL=0, MIN=0, MAX=1 (dtype=BOOL)
              'SP,NR=2' [Rueckruferlaubnis]: VAL=1, MIN=0, MAX=1 (dtype=BOOL)
              ...
              write data to: /home/pi/prog/htheatpump/htparams-123456-3_0_20-273.csv
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
        "-c",
        "--csv",
        type=str,
        help="write the result to the specified CSV file",
        nargs="?",
        const="",
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
        "--max-retries",
        default=2,
        type=int,
        choices=range(11),
        help="maximum number of retries for a data point request (0..10), default: %(default)s",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        # Use the default timeout defined in the HtHeatpump class
        default=AioHtHeatpump.DEFAULT_TIMEOUT,
        help="connection timeout in seconds, default: %(default)s",
    )

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
        print("connected successfully to heat pump with serial number {:d}".format(rid))
        ver = await hp.get_version_async()
        print("software version = {} ({:d})".format(ver[0], ver[1]))

        result: Dict[str, Dict[int, Dict[str, Any]]] = {}
        with Timer() as timer:
            for dp_type in ("SP", "MP"):  # for all known data point types
                result.update({dp_type: {}})
                i = 0  # start at zero for each data point type
                while True:
                    success = False
                    retry = 0
                    while not success and retry <= args.max_retries:
                        data_point = "{},NR={:d}".format(dp_type, i)
                        # send request for data point to the heat pump
                        await hp.send_request_async(data_point)
                        # ... and wait for the response
                        try:
                            resp = await hp.read_response_async()
                            # search for pattern "NAME=...", "VAL=...", "MAX=..." and "MIN=..." inside the answer
                            m = re.match(
                                r"^{},.*NAME=([^,]+).*VAL=([^,]+).*MAX=([^,]+).*MIN=([^,]+).*$".format(
                                    data_point
                                ),
                                resp,
                            )
                            if not m:
                                raise IOError(
                                    "invalid response for query of data point {!r} [{}]".format(
                                        data_point, resp
                                    )
                                )
                            # extract name, value, min and max
                            name, value, min_val, max_val = (
                                g.strip() for g in m.group(1, 2, 4, 3)
                            )
                            # determine the data type of the data point
                            dtype = None
                            try:
                                min_val = HtParam.from_str(min_val, HtDataTypes.INT)
                                max_val = HtParam.from_str(max_val, HtDataTypes.INT)
                                value = HtParam.from_str(value, HtDataTypes.INT)
                                dtype = "INT"
                                if min_val == 0 and max_val == 1 and value in (0, 1):
                                    dtype = "BOOL"
                            except ValueError:
                                min_val = HtParam.from_str(
                                    min_val, HtDataTypes.FLOAT, strict=False
                                )
                                max_val = HtParam.from_str(
                                    max_val, HtDataTypes.FLOAT, strict=False
                                )
                                value = HtParam.from_str(
                                    value, HtDataTypes.FLOAT, strict=False
                                )
                                dtype = "FLOAT"
                            assert dtype is not None
                            # print the determined values
                            print(
                                "{!r} [{}]: VAL={}, MIN={}, MAX={} (dtype={})".format(
                                    data_point, name, value, min_val, max_val, dtype
                                )
                            )
                            # store the determined data in the result dict
                            result[dp_type].update(
                                {
                                    i: {
                                        "name": name,
                                        "value": value,
                                        "min": min_val,
                                        "max": max_val,
                                        "dtype": dtype,
                                    }
                                }
                            )
                            success = True
                        except Exception as ex:
                            retry += 1
                            _LOGGER.warning(
                                "try #%d/%d for query of data point '%s' failed: %s",
                                retry,
                                args.max_retries + 1,
                                data_point,
                                ex,
                            )
                            # try a reconnect, maybe this will help
                            await hp.reconnect_async()  # perform a reconnect
                            try:
                                await hp.login_async(
                                    max_retries=0
                                )  # ... and a new login
                            except Exception:
                                pass  # ignore a potential problem
                    if not success:
                        _LOGGER.error(
                            "query of data point '%s' failed after %d try/tries",
                            data_point,
                            retry,
                        )
                        break
                    else:
                        i += 1
        exec_time = timer.elapsed

        if args.csv is not None:  # write result to CSV file
            filename = args.csv.strip()
            if filename == "":
                filename = os.path.join(
                    os.getcwd(),
                    "htparams-{}-{}-{}.csv".format(
                        rid, ver[0].replace(".", "_"), ver[1]
                    ),
                )
            print("write data to: " + filename)
            with open(filename, "w", encoding="utf-8") as csvfile:
                header = (
                    "# name",
                    "data point type (MP;SP)",
                    "data point number",
                    "acl (r-;-w;rw)",
                    "dtype (BOOL;INT;FLOAT)",
                    "min",
                    "max",
                )
                writer = csv.writer(csvfile, delimiter=",")
                writer.writerow(header)
                for dp_type, content in sorted(result.items()):
                    for i, data in content.items():
                        row_data = (
                            data["name"],
                            dp_type,
                            str(i),
                            "r-",
                            data["dtype"],
                            str(data["min"]),
                            str(data["max"]),
                        )
                        writer.writerow(row_data)

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
