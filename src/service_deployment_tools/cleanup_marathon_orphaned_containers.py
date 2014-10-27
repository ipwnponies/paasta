#!/usr/bin/env python
"""
Usage: cleanup_marathon_orphaned_containers.py [options]
"""

import argparse
import calendar
import datetime
import logging

import docker

log = logging.getLogger('__main__')


def get_running_containers(client):
    """Given a docker-py Docker client, return the docker containers running on
    this machine (docker ps).
    """
    return client.containers()


def get_mesos_containers(containers):
    """Given a list of Docker containers as from get_running_containers(),
    return a list of the containers started by Mesos.
    """
    mesos_containers = []
    for image in containers:
        if any([name for name in image.get('Names', []) if name.startswith('/mesos-')]):
            mesos_containers.append(image)
    return mesos_containers


def get_old_containers(containers, max_age=60, now=None):
    """Given a list of Docker containers as from get_running_containers(),
    return a list of the containers started more than max_age ago.
    """
    age_delta = datetime.timedelta(minutes=max_age)
    if now is None:
        now = datetime.datetime.now()
    max_age_timestamp = calendar.timegm((now + age_delta).timetuple())

    return [image for image in containers if image.get('Created', 0) > max_age_timestamp]


def parse_args():
    parser = argparse.ArgumentParser(
        description='Stop Docker containers spawned by Mesos which are no longer supposed to be running')
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='verbose',
        default=False,
    )
    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    if args.verbose:
        log.setLevel(logging.INFO)
    else:
        log.setLevel(logging.WARNING)

    client = docker.Client()
    from pprint import pprint
    running_containers = get_running_containers(client)
    pprint(running_containers)
    mesos_containers = get_mesos_containers(running_containers)
    old_containers = get_old_containers(mesos_containers)
    pprint(old_containers)


if __name__ == "__main__":
    main()
