#!/usr/bin/env python
# Copyright 2015-2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import copy
import itertools
import sys
from collections import Counter
from collections import namedtuple
from collections import OrderedDict

from httplib2 import ServerNotFoundError
from marathon.exceptions import MarathonError

from paasta_tools import chronos_tools
from paasta_tools import marathon_tools
from paasta_tools.chronos_tools import ChronosNotConfigured
from paasta_tools.chronos_tools import get_chronos_client
from paasta_tools.chronos_tools import load_chronos_config
from paasta_tools.marathon_tools import MarathonNotConfigured
from paasta_tools.mesos_tools import get_all_tasks_from_state
from paasta_tools.mesos_tools import get_mesos_quorum
from paasta_tools.mesos_tools import get_mesos_state_from_leader
from paasta_tools.mesos_tools import get_mesos_stats
from paasta_tools.mesos_tools import get_number_of_mesos_masters
from paasta_tools.mesos_tools import get_zookeeper_config
from paasta_tools.mesos_tools import MasterNotAvailableException
from paasta_tools.utils import PaastaColors
from paasta_tools.utils import print_with_indent
from paasta_tools.utils import format_table

HealthCheckResult = namedtuple('HealthCheckResult', ['message', 'healthy'])
ResourceInfo = namedtuple('ResourceInfo', ['cpus', 'mem', 'disk'])
ResourceUtilization = namedtuple('ResourceUtilization', ['metric', 'total', 'free'])


def parse_args():
    parser = argparse.ArgumentParser(
        description='',
    )
    parser.add_argument(
        '-g',
        '--groupings',
        nargs='+',
        default=['region'],
        help=(
            'Group resource information of slaves grouped by attribute.'
            'Note: This is only effective with -vv'
        )
    )
    parser.add_argument('-t', '--threshold', type=int, default=90)
    parser.add_argument('-v', '--verbose', action='count', dest="verbose", default=0,
                        help="Print out more output regarding the state of the cluster")
    parser.add_argument('-H', '--humanize', action='store_true', dest="humanize", default=False,
                        help="Print human-readable sizes")
    return parser.parse_args()


def get_num_masters(state):
    """ Gets the number of masters from mesos state """
    return get_number_of_mesos_masters(get_zookeeper_config(state))


def get_mesos_cpu_status(metrics):
    """Takes in the mesos metrics and analyzes them, returning the status
    :param metrics: mesos metrics dictionary
    :returns: Tuple of the output array and is_ok bool
    """

    total = metrics['master/cpus_total']
    used = metrics['master/cpus_used']
    available = total - used
    return total, used, available


def get_mesos_disk_status(metrics):
    """Takes in the mesos metrics and analyzes them, returning the status
    :param metrics: mesos metrics dictionary
    :returns: Tuple of the output array and is_ok bool
    """

    total = metrics['master/disk_total']
    used = metrics['master/disk_used']
    available = total - used
    return total, used, available


def filter_mesos_state_metrics(dictionary):
    valid_keys = ['cpus', 'mem', 'disk']
    return {key: value for (key, value) in dictionary.items() if key in valid_keys}


def healthcheck_result_for_resource_utilization(resource_utilization, threshold):
    """
    Given a resource data dict, assert that cpu
    data is ok.
    :param resource_utilization: the resource_utilization tuple to check
    :returns: a HealthCheckResult
    """
    utilization = percent_used(resource_utilization.total, resource_utilization.total - resource_utilization.free)
    message = "%s: %.2f/%.2f(%.2f%%) used. Threshold (%.2f%%)" % (
        resource_utilization.metric,
        float(resource_utilization.total - resource_utilization.free),
        resource_utilization.total,
        utilization,
        threshold,
    )
    healthy = utilization <= threshold
    return HealthCheckResult(
        message=message,
        healthy=healthy
    )


def quorum_ok(masters, quorum):
    return masters >= quorum


def check_threshold(percent_used, threshold):
    return (100 - percent_used) > threshold


def percent_used(total, used):
    return round(used / float(total) * 100.0, 2)


def assert_cpu_health(metrics, threshold=10):
    total, used, available = get_mesos_cpu_status(metrics)
    try:
        perc_used = percent_used(total, used)
    except ZeroDivisionError:
        return HealthCheckResult(message="Error reading total available cpu from mesos!",
                                 healthy=False)

    if check_threshold(perc_used, threshold):
        return HealthCheckResult(message="CPUs: %.2f / %d in use (%s)"
                                 % (used, total, PaastaColors.green("%.2f%%" % perc_used)),
                                 healthy=True)
    else:
        return HealthCheckResult(message="CRITICAL: Less than %d%% CPUs available. (Currently using %.2f%% of %d)"
                                 % (threshold, perc_used, total),
                                 healthy=False)


def assert_memory_health(metrics, threshold=10):
    total = metrics['master/mem_total'] / float(1024)
    used = metrics['master/mem_used'] / float(1024)
    try:
        perc_used = percent_used(total, used)
    except ZeroDivisionError:
        return HealthCheckResult(message="Error reading total available memory from mesos!",
                                 healthy=False)

    if check_threshold(perc_used, threshold):
        return HealthCheckResult(
            message="Memory: %0.2f / %0.2fGB in use (%s)"
            % (used, total, PaastaColors.green("%.2f%%" % perc_used)),
            healthy=True
        )
    else:
        return HealthCheckResult(
            message="CRITICAL: Less than %d%% memory available. (Currently using %.2f%% of %.2fGB)"
                    % (threshold, perc_used, total),
                    healthy=False
        )


def assert_disk_health(metrics, threshold=10):
    total = metrics['master/disk_total'] / float(1024)
    used = metrics['master/disk_used'] / float(1024)
    try:
        perc_used = percent_used(total, used)
    except ZeroDivisionError:
        return HealthCheckResult(message="Error reading total available disk from mesos!",
                                 healthy=False)

    if check_threshold(perc_used, threshold):
        return HealthCheckResult(
            message="Disk: %0.2f / %0.2fGB in use (%s)"
            % (used, total, PaastaColors.green("%.2f%%" % perc_used)),
            healthy=True
        )
    else:
        return HealthCheckResult(
            message="CRITICAL: Less than %d%% disk available. (Currently using %.2f%%)" % (threshold, perc_used),
            healthy=False
        )


def assert_tasks_running(metrics):
    running = metrics['master/tasks_running']
    staging = metrics['master/tasks_staging']
    starting = metrics['master/tasks_starting']
    return HealthCheckResult(
        message="Tasks: running: %d staging: %d starting: %d" % (running, staging, starting),
        healthy=True
    )


def assert_no_duplicate_frameworks(state):
    """A function which asserts that there are no duplicate frameworks running, where
    frameworks are identified by their name.

    Note the extra spaces in the output strings: this is to account for the extra indentation
    we add, so we can have:

        frameworks:
          framework: marathon count: 1

    :param state: the state info from the Mesos master
    :returns: a tuple containing (output, ok): output is a log of the state of frameworks, ok a boolean
        indicating if there are any duplicate frameworks.
    """
    frameworks = state['frameworks']
    framework_counts = OrderedDict(sorted(Counter([fw['name'] for fw in frameworks]).items()))
    output = ["Frameworks:"]
    ok = True

    for framework, count in framework_counts.iteritems():
        if count > 1:
            ok = False
            output.append("    CRITICAL: Framework %s has %d instances running--expected no more than 1."
                          % (framework, count))
        else:
            output.append("    Framework: %s count: %d" % (framework, count))
    return HealthCheckResult(
        message=("\n").join(output),
        healthy=ok
    )


def assert_slave_health(metrics):
    active, inactive = metrics['master/slaves_active'], metrics['master/slaves_inactive']
    return HealthCheckResult(
        message="Slaves: active: %d inactive: %d" % (active, inactive),
        healthy=True
    )


def assert_quorum_size(state):
    masters, quorum = get_num_masters(state), get_mesos_quorum(state)
    if quorum_ok(masters, quorum):
        return HealthCheckResult(
            message="Quorum: masters: %d configured quorum: %d " % (masters, quorum),
            healthy=True
        )
    else:
        return HealthCheckResult(
            message="CRITICAL: Number of masters (%d) less than configured quorum(%d)." % (masters, quorum),
            healthy=False
        )


def group_slaves_by_attribute(slaves, attribute):
    """
    Given the state information provided by Mesos and a mesos attribute, group the slaves
    by that attribute.

    :param slaves: a list of slave objects as defined in mesos state.
    <https://mesos.apache.org/documentation/latest/endpoints/master/state.json/>``.
    :param attribute: the attribute to group slaves by. This attribute must be common to all slaves.
    :returns: groupby object, with slaves grouped by the value of the attribute
    """
    def key_func(slave):
        return slave['attributes'].get(attribute, 'unknown')
    sorted_slaves = sorted(slaves, key=key_func)
    return itertools.groupby(sorted_slaves, key=key_func)


def get_resource_utilization_per_slave(mesos_state):
    """
    Given a mesos state object, calculate the resource utilization
    of each individual slave.
    :param mesos_stage: the mesos state object
    :returns: a dict of {hostname: free: ResourceInfo, total: ResourceInfo}
    """
    slaves = dict((slave['id'], {
        'hostname': slave['hostname'],
        'total_resources': Counter(filter_mesos_state_metrics(slave['resources'])),
        'free_resources': Counter(filter_mesos_state_metrics(slave['resources'])),
    }) for slave in mesos_state['slaves'])

    for framework in mesos_state.get('frameworks', []):
        for task in framework.get('tasks', []):
            mesos_metrics = filter_mesos_state_metrics(task['resources'])
            slaves[task['slave_id']]['free_resources'].subtract(mesos_metrics)

    formatted_slaves = {slave['hostname']: {
        'free': ResourceInfo(
            cpus=slave['free_resources']['cpus'],
            mem=slave['free_resources']['mem'],
            disk=slave['free_resources']['disk'],
        ),
        'total': ResourceInfo(
            cpus=slave['total_resources']['cpus'],
            mem=slave['total_resources']['mem'],
            disk=slave['total_resources']['disk'],
        )
    } for slave in slaves.values()}
    return formatted_slaves


def calculate_resource_utilization_for_slaves(slaves, tasks):
    """
    Given a list of slaves and a list of tasks, calculate the total available
    resource available in that list of slaves, and the resources consumed by tasks
    running on those slaves.

    :param slaves: a list of slaves to calculate resource usage for
    :param tasks: the list of tasks running in the mesos cluster
    :returns: a dict, containing keys for "free" and "total" resources. Each of these keys
    is a ResourceInfo tuple, exposing a number for cpu, disk and mem.
    """
    resource_total_dict = Counter()
    for slave in slaves:
        filtered_resources = filter_mesos_state_metrics(slave['resources'])
        resource_total_dict.update(Counter(filtered_resources))
    resource_free_dict = copy.deepcopy(resource_total_dict)
    for task in tasks:
        task_resources = task['resources']
        resource_free_dict.subtract(Counter(filter_mesos_state_metrics(task_resources)))
    return {
        "free": ResourceInfo(
            cpus=resource_free_dict['cpus'],
            disk=resource_free_dict['disk'],
            mem=resource_free_dict['mem']
        ),
        "total": ResourceInfo(
            cpus=resource_total_dict['cpus'],
            disk=resource_total_dict['disk'],
            mem=resource_total_dict['mem'],
        )
    }


def get_resource_utilization_by_attribute(mesos_state, attribute):
    """
    Given mesos state and an attribute, calculate resource utilization
    for each value of a given attribute.

    :param mesost_state: the mesos state
    :param attribute: the attribute to group slaves by
    :returns: a dict of {attribute_value: resource_usage}, where resource usage is
    the dict returned by ``calculate_resource_utilization_for_slaves`` for slaves
    grouped by attribute value.
    """
    slaves = mesos_state.get('slaves', [])
    if not has_registered_slaves(mesos_state):
        raise ValueError("There are no slaves registered in the mesos state.")

    tasks = get_all_tasks_from_state(mesos_state)
    slave_groupings = group_slaves_by_attribute(slaves, attribute)

    return {
        attribute_value: calculate_resource_utilization_for_slaves(slaves, tasks)
        for attribute_value, slaves in slave_groupings
    }


def resource_utillizations_from_resource_info(total, free):
    """
    Given two ResourceInfo tuples, one for total and one for free,
    create a ResourceUtilization tuple for each metric in the ResourceInfo.
    :param total:
    :param free:
    :returns: ResourceInfo for a metric
    """
    return [
        ResourceUtilization(metric=field, total=total[index], free=free[index])
        for index, field in enumerate(ResourceInfo._fields)
    ]


def has_registered_slaves(mesos_state):
    return 'slaves' in mesos_state and mesos_state['slaves']


def get_mesos_metrics_health(mesos_metrics):
    """Perform healthchecks against mesos metrics.
    :param mesos_metrics: a dict exposing the mesos metrics described in
    https://mesos.apache.org/documentation/latest/monitoring/
    :returns: a list of HealthCheckResult tuples
    """
    metrics_results = run_healthchecks_with_param(mesos_metrics, [
        assert_cpu_health,
        assert_memory_health,
        assert_disk_health,
        assert_tasks_running,
        assert_slave_health,
    ])
    return metrics_results


def get_mesos_state_status(mesos_state):
    """Perform healthchecks against mesos state.
    :param mesos_state: a dict exposing the mesos state described in
    https://mesos.apache.org/documentation/latest/endpoints/master/state.json/
    :returns: a list of HealthCheckResult tuples
    """
    cluster_results = run_healthchecks_with_param(
        mesos_state,
        [assert_quorum_size, assert_no_duplicate_frameworks]
    )
    return cluster_results


def run_healthchecks_with_param(param, healthcheck_functions, format_options={}):
    return [healthcheck(param, **format_options) for healthcheck in healthcheck_functions]


def assert_marathon_apps(client):
    num_apps = len(client.list_apps())
    if num_apps < 1:
        return HealthCheckResult(message="CRITICAL: No marathon apps running",
                                 healthy=False)
    else:
        return HealthCheckResult(message="marathon apps: %d" % num_apps, healthy=True)


def assert_marathon_tasks(client):
    num_tasks = len(client.list_tasks())
    return HealthCheckResult(message="marathon tasks: %d" % num_tasks, healthy=True)


def assert_marathon_deployments(client):
    num_deployments = len(client.list_deployments())
    return HealthCheckResult(message="marathon deployments: %d" % num_deployments, healthy=True)


def get_marathon_status(client):
    """ Gathers information about marathon.
    :return: string containing the status.  """
    return run_healthchecks_with_param(client, [
        assert_marathon_apps,
        assert_marathon_tasks,
        assert_marathon_deployments])


def assert_chronos_scheduled_jobs(client):
    """
    :returns: a tuple of a string and a bool containing representing if it is ok or not
    """
    num_jobs = len(chronos_tools.filter_enabled_jobs(client.list()))
    return HealthCheckResult(message="Enabled chronos jobs: %d" % num_jobs, healthy=True)


def get_chronos_status(chronos_client):
    """Gather information about chronos.
    :return: string containing the status
    """
    return run_healthchecks_with_param(chronos_client, [
        assert_chronos_scheduled_jobs,
    ])


def get_marathon_client(marathon_config):
    """Given a MarathonConfig object, return
    a client.
    :param marathon_config: a MarathonConfig object
    :returns client: a marathon client
    """
    return marathon_tools.get_marathon_client(
        marathon_config.get_url(),
        marathon_config.get_username(),
        marathon_config.get_password()
    )


def critical_events_in_outputs(healthcheck_outputs):
    """Given a list of HealthCheckResults return those which are unhealthy.
    """
    return [healthcheck for healthcheck in healthcheck_outputs if healthcheck.healthy is False]


def generate_summary_for_check(name, ok):
    """Given a check name and a boolean indicating if the service is OK, return
    a formatted message.
    """
    status = PaastaColors.green("OK") if ok is True else PaastaColors.red("CRITICAL")
    summary = "%s Status: %s" % (name, status)
    return summary


def status_for_results(healthcheck_results):
    """Given a list of HealthCheckResult tuples, return the ok status
    for each one.
    :param healthcheck_results: a list of HealthCheckResult tuples
    :returns: a list of booleans.
    """
    return [result.healthy for result in healthcheck_results]


def print_results_for_healthchecks(ok, results, verbose, indent=2):
    if verbose >= 1:
        for health_check_result in results:
            if health_check_result.healthy:
                print_with_indent(health_check_result.message, indent)
            else:
                print_with_indent(PaastaColors.red(health_check_result.message), indent)
    elif not ok:
        unhealthy_results = critical_events_in_outputs(results)
        for health_check_result in unhealthy_results:
            print_with_indent(PaastaColors.red(health_check_result.message), 2)


def main():
    marathon_config = None
    chronos_config = None
    args = parse_args()

    try:
        mesos_state = get_mesos_state_from_leader()
    except MasterNotAvailableException as e:
        # if we can't connect to master at all,
        # then bomb out early
        print(PaastaColors.red("CRITICAL:  %s" % e.message))
        sys.exit(2)

    mesos_state_status = get_mesos_state_status(
        mesos_state=mesos_state,
    )
    metrics = get_mesos_stats()
    mesos_metrics_status = get_mesos_metrics_health(mesos_metrics=metrics)

    all_mesos_results = mesos_state_status + mesos_metrics_status

    # Check to see if Marathon should be running here by checking for config
    try:
        marathon_config = marathon_tools.load_marathon_config()
    except MarathonNotConfigured:
        marathon_results = [HealthCheckResult(message='Marathon is not configured to run here', healthy=True)]

    # Check to see if Chronos should be running here by checking for config
    try:
        chronos_config = load_chronos_config()
    except ChronosNotConfigured:
        chronos_results = [HealthCheckResult(message='Chronos is not configured to run here', healthy=True)]

    if marathon_config:
        marathon_client = get_marathon_client(marathon_config)
        try:
            marathon_results = get_marathon_status(marathon_client)
        except MarathonError as e:
            print(PaastaColors.red("CRITICAL: Unable to contact Marathon! Error: %s" % e))
            sys.exit(2)

    if chronos_config:
        chronos_client = get_chronos_client(chronos_config)
        try:
            chronos_results = get_chronos_status(chronos_client)
        except ServerNotFoundError as e:
            print(PaastaColors.red("CRITICAL: Unable to contact Chronos! Error: %s" % e))
            sys.exit(2)

    mesos_ok = all(status_for_results(all_mesos_results))
    marathon_ok = all(status_for_results(marathon_results))
    chronos_ok = all(status_for_results(chronos_results))

    mesos_summary = generate_summary_for_check("Mesos", mesos_ok)
    marathon_summary = generate_summary_for_check("Marathon", marathon_ok)
    chronos_summary = generate_summary_for_check("Chronos", chronos_ok)

    if args.verbose == 0:
        print mesos_summary
        print marathon_summary
        print chronos_summary
    elif args.verbose == 1:
        print mesos_summary
        print_results_for_healthchecks(mesos_ok, all_mesos_results, args.verbose)
        print marathon_summary
        print_results_for_healthchecks(marathon_ok, marathon_results, args.verbose)
        print chronos_summary
        print_results_for_healthchecks(chronos_ok, chronos_results, args.verbose)
    elif args.verbose == 2:
        print mesos_summary
        print_results_for_healthchecks(mesos_ok, all_mesos_results, args.verbose)
        for attribute in args.groupings:
            for attribute_value, resource_usage_dict in get_resource_utilization_by_attribute(mesos_state, attribute):
                resource_utilizations = resource_utillizations_from_resource_info(
                    resource_usage_dict['total'],
                    resource_usage_dict['free'],
                )
                healthcheck_results = [
                    healthcheck_result_for_utilization(utilization, args.threshold)
                    for utilization in resource_utilizations
                ]
            print 'Cluster Utilization, grouped by: %s' % attribute
            print_with_indent(attribute_value, 2)
            print_results_for_healthchecks(
                all(status_for_results(healthcheck_results)),
                healthcheck_results,
                args.verbose
            )
        print marathon_summary
        print chronos_summary
    # else:
        # print mesos_summary
        # # print results + extra attribute data + extra slave data
        # for message in [check.message for check in all_mesos_results + [extra_attribute_data]]:
        # print_with_indent(message)

    if not all([mesos_ok, marathon_ok, chronos_ok]):
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == '__main__':
    main()
