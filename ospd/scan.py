# Copyright (C) 2020 Greenbone Networks GmbH
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.

import logging
import multiprocessing
import time
import uuid

from collections import OrderedDict
from enum import Enum
from typing import List, Any, Dict, Iterator, Optional

from ospd.network import target_str_to_list

LOGGER = logging.getLogger(__name__)


class ScanStatus(Enum):
    """Scan status. """

    INIT = 0
    RUNNING = 1
    STOPPED = 2
    FINISHED = 3


class ScanCollection:

    """ Scans collection, managing scans and results read and write, exposing
    only needed information.

    Each scan has meta-information such as scan ID, current progress (from 0 to
    100), start time, end time, scan target and options and a list of results.

    There are 4 types of results: Alarms, Logs, Errors and Host Details.

    Todo:
    - Better checking for Scan ID existence and handling otherwise.
    - More data validation.
    - Mutex access per table/scan_info.

    """

    def __init__(self) -> None:
        """ Initialize the Scan Collection. """

        self.data_manager = (
            None
        )  # type: Optional[multiprocessing.managers.SyncManager]
        self.scans_table = dict()  # type: Dict

    def add_result(
        self,
        scan_id: str,
        result_type: int,
        host: str = '',
        hostname: str = '',
        name: str = '',
        value: str = '',
        port: str = '',
        test_id: str = '',
        severity: str = '',
        qod: str = '',
    ) -> None:
        """ Add a result to a scan in the table. """

        assert scan_id
        assert len(name) or len(value)

        result = OrderedDict()  # type: Dict
        result['type'] = result_type
        result['name'] = name
        result['severity'] = severity
        result['test_id'] = test_id
        result['value'] = value
        result['host'] = host
        result['hostname'] = hostname
        result['port'] = port
        result['qod'] = qod
        results = self.scans_table[scan_id]['results']
        results.append(result)

        # Set scan_info's results to propagate results to parent process.
        self.scans_table[scan_id]['results'] = results

    def remove_hosts_from_target_progress(
        self, scan_id: str, hosts: List
    ) -> None:
        """Remove a list of hosts from the main scan progress table to avoid
        the hosts to be included in the calculation of the scan progress"""
        if not hosts:
            return

        target = self.scans_table[scan_id].get('target_progress')
        for host in hosts:
            if host in target:
                del target[host]

        # Set scan_info's target_progress to propagate progresses
        # to parent process.
        self.scans_table[scan_id]['target_progress'] = target

    def set_progress(self, scan_id: str, progress: int) -> None:
        """ Sets scan_id scan's progress. """

        if progress > 0 and progress <= 100:
            self.scans_table[scan_id]['progress'] = progress

        if progress == 100:
            self.scans_table[scan_id]['end_time'] = int(time.time())

    def set_host_progress(self, scan_id: str, host: str, progress: int) -> None:
        """ Sets scan_id scan's progress. """
        if progress > 0 and progress <= 100:
            host_progresses = self.scans_table[scan_id].get('target_progress')
            host_progresses[host] = progress
            # Set scan_info's target_progress to propagate progresses
            # to parent process.
            self.scans_table[scan_id]['target_progress'] = host_progresses

    def set_host_finished(self, scan_id: str, host: str) -> None:
        """ Add the host in a list of finished hosts """
        finished_hosts = self.scans_table[scan_id].get('finished_hosts')

        if host not in finished_hosts:
            finished_hosts.append(host)

        self.scans_table[scan_id]['finished_hosts'] = finished_hosts

    def get_hosts_unfinished(self, scan_id: str) -> List[Any]:
        """ Get a list of unfinished hosts."""

        unfinished_hosts = target_str_to_list(self.get_host_list(scan_id))

        finished_hosts = self.get_hosts_finished(scan_id)

        for host in finished_hosts:
            unfinished_hosts.remove(host)

        return unfinished_hosts

    def get_hosts_finished(self, scan_id: str) -> List:
        """ Get a list of finished hosts."""

        return self.scans_table[scan_id].get('finished_hosts')

    def results_iterator(
        self, scan_id: str, pop_res: bool = False, max_res: int = None
    ) -> Iterator[Any]:
        """ Returns an iterator over scan_id scan's results. If pop_res is True,
        it removed the fetched results from the list.

        If max_res is None, return all the results.
        Otherwise, if max_res = N > 0 return N as maximum number of results.

        max_res works only together with pop_results.
        """
        if pop_res and max_res:
            result_aux = self.scans_table[scan_id]['results']
            self.scans_table[scan_id]['results'] = result_aux[max_res:]
            return iter(result_aux[:max_res])
        elif pop_res:
            result_aux = self.scans_table[scan_id]['results']
            self.scans_table[scan_id]['results'] = list()
            return iter(result_aux)

        return iter(self.scans_table[scan_id]['results'])

    def ids_iterator(self) -> Iterator[str]:
        """ Returns an iterator over the collection's scan IDS. """

        return iter(self.scans_table.keys())

    def remove_single_result(
        self, scan_id: str, result: Dict[str, str]
    ) -> None:
        """Removes a single result from the result list in scan_table.

        Parameters:
            scan_id (uuid): Scan ID to identify the scan process to be resumed.
            result (dict): The result to be removed from the results list.
        """
        results = self.scans_table[scan_id]['results']
        results.remove(result)
        self.scans_table[scan_id]['results'] = results

    def del_results_for_stopped_hosts(self, scan_id: str) -> None:
        """ Remove results from the result table for those host
        """
        unfinished_hosts = self.get_hosts_unfinished(scan_id)
        for result in self.results_iterator(
            scan_id, pop_res=False, max_res=None
        ):
            if result['host'] in unfinished_hosts:
                self.remove_single_result(scan_id, result)

    def resume_scan(self, scan_id: str, options: Optional[Dict]) -> str:
        """ Reset the scan status in the scan_table to INIT.
        Also, overwrite the options, because a resume task cmd
        can add some new option. E.g. exclude hosts list.
        Parameters:
            scan_id (uuid): Scan ID to identify the scan process to be resumed.
            options (dict): Options for the scan to be resumed. This options
                            are not added to the already existent ones.
                            The old ones are removed

        Return:
            Scan ID which identifies the current scan.
        """
        self.scans_table[scan_id]['status'] = ScanStatus.INIT
        if options:
            self.scans_table[scan_id]['options'] = options

        self.del_results_for_stopped_hosts(scan_id)

        return scan_id

    def create_scan(
        self,
        scan_id: str = '',
        target: Dict = None,
        options: Optional[Dict] = None,
        vts: Dict = None,
    ) -> str:
        """ Creates a new scan with provided scan information. """

        if not target:
            target = {}

        if self.data_manager is None:
            self.data_manager = multiprocessing.Manager()

        # Check if it is possible to resume task. To avoid to resume, the
        # scan must be deleted from the scans_table.
        if (
            scan_id
            and self.id_exists(scan_id)
            and (self.get_status(scan_id) == ScanStatus.STOPPED)
        ):
            self.scans_table[scan_id]['end_time'] = 0

            return self.resume_scan(scan_id, options)

        if not options:
            options = dict()

        scan_info = self.data_manager.dict()  # type: Dict
        scan_info['results'] = list()
        scan_info['finished_hosts'] = list()
        scan_info['progress'] = 0
        scan_info['target_progress'] = dict()
        scan_info['target'] = target
        scan_info['vts'] = vts
        scan_info['options'] = options
        scan_info['start_time'] = int(time.time())
        scan_info['end_time'] = 0
        scan_info['status'] = ScanStatus.INIT

        if scan_id is None or scan_id == '':
            scan_id = str(uuid.uuid4())

        scan_info['scan_id'] = scan_id

        self.scans_table[scan_id] = scan_info
        return scan_id

    def set_status(self, scan_id: str, status: ScanStatus) -> None:
        """ Sets scan_id scan's status. """
        self.scans_table[scan_id]['status'] = status
        if status == ScanStatus.STOPPED:
            self.scans_table[scan_id]['end_time'] = int(time.time())

    def get_status(self, scan_id: str) -> ScanStatus:
        """ Get scan_id scans's status."""

        return self.scans_table[scan_id]['status']

    def get_options(self, scan_id: str) -> Dict:
        """ Get scan_id scan's options list. """

        return self.scans_table[scan_id]['options']

    def set_option(self, scan_id, name: str, value: Any) -> None:
        """ Set a scan_id scan's name option to value. """

        self.scans_table[scan_id]['options'][name] = value

    def get_progress(self, scan_id: str) -> int:
        """ Get a scan's current progress value. """

        return self.scans_table[scan_id]['progress']

    def simplify_exclude_host_list(self, scan_id: str) -> List[Any]:
        """ Remove from exclude_hosts the received hosts in the finished_hosts
        list sent by the client.
        The finished hosts are sent also as exclude hosts for backward
        compatibility purposses.
        """

        exc_hosts_list = target_str_to_list(self.get_exclude_hosts(scan_id))

        finished_hosts_list = target_str_to_list(
            self.get_finished_hosts(scan_id)
        )

        if finished_hosts_list and exc_hosts_list:
            for finished in finished_hosts_list:
                if finished in exc_hosts_list:
                    exc_hosts_list.remove(finished)

        return exc_hosts_list

    def calculate_target_progress(self, scan_id: str) -> float:
        """ Get a target's current progress value.
        The value is calculated with the progress of each single host
        in the target."""

        host = self.get_host_list(scan_id)
        total_hosts = len(target_str_to_list(host))
        exc_hosts_list = self.simplify_exclude_host_list(scan_id)
        exc_hosts = len(exc_hosts_list) if exc_hosts_list else 0
        host_progresses = self.scans_table[scan_id].get('target_progress')

        try:
            t_prog = sum(host_progresses.values()) / (
                total_hosts - exc_hosts
            )  # type: float
        except ZeroDivisionError:
            LOGGER.error(
                "Zero division error in %s",
                self.calculate_target_progress.__name__,
            )
            raise

        return t_prog

    def get_start_time(self, scan_id: str) -> str:
        """ Get a scan's start time. """

        return self.scans_table[scan_id]['start_time']

    def get_end_time(self, scan_id: str) -> str:
        """ Get a scan's end time. """

        return self.scans_table[scan_id]['end_time']

    def get_host_list(self, scan_id: str) -> Dict:
        """ Get a scan's host list. """

        return self.scans_table[scan_id]['target'].get('hosts')

    def get_ports(self, scan_id: str):
        """ Get a scan's ports list.
        """
        return self.scans_table[scan_id]['target'].get('ports')

    def get_exclude_hosts(self, scan_id: str):
        """ Get an exclude host list for a given target.
        """
        return self.scans_table[scan_id]['target'].get('exclude_hosts')

    def get_finished_hosts(self, scan_id: str):
        """ Get the finished host list sent by the client for a given target.
        """
        return self.scans_table[scan_id]['target'].get('finished_hosts')

    def get_credentials(self, scan_id: str):
        """ Get a scan's credential list. It return dictionary with
        the corresponding credential for a given target.
        """
        return self.scans_table[scan_id]['target'].get('credentials')

    def get_target_options(self, scan_id: str):
        """ Get a scan's target option dictionary.
        It return dictionary with the corresponding options for
        a given target.
        """
        return self.scans_table[scan_id]['target'].get('options')

    def get_vts(self, scan_id: str) -> Dict:
        """ Get a scan's vts. """

        return self.scans_table[scan_id]['vts']

    def release_vts_list(self, scan_id: str) -> None:
        """ Release the memory used for the vts list. """

        scan_data = self.scans_table.get(scan_id)
        if scan_data and 'vts' in scan_data:
            del scan_data['vts']

    def id_exists(self, scan_id: str) -> bool:
        """ Check whether a scan exists in the table. """

        return self.scans_table.get(scan_id) is not None

    def delete_scan(self, scan_id: str) -> bool:
        """ Delete a scan if fully finished. """

        if self.get_status(scan_id) == ScanStatus.RUNNING:
            return False

        self.scans_table.pop(scan_id)

        if len(self.scans_table) == 0:
            del self.data_manager
            self.data_manager = None

        return True
