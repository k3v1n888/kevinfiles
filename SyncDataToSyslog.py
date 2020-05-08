import os
import logging
from logging.handlers import RotatingFileHandler
from aliyun.log.consumer import *
from aliyun.log.pulllog_response import PullLogResponse
from multiprocessing import current_process
import aliyun.log.ext.syslogclient as syslogclient
from aliyun.log.ext.syslogclient import SyslogClientRFC5424 as SyslogClient
import six
from datetime import datetime
import socket
import requests
from concurrent.futures import ThreadPoolExecutor
import json

# configure logging file
root = logging.getLogger()
handler = RotatingFileHandler("{0}_{1}.log".format(os.path.basename(__file__), current_process().pid), maxBytes=100*1024*1024, backupCount=5)
handler.setFormatter(logging.Formatter(fmt='[%(asctime)s] - [%(threadName)s] - {%(module)s:%(funcName)s:%(lineno)d} %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
root.setLevel(logging.INFO)
root.addHandler(handler)
root.addHandler(logging.StreamHandler())

logger = logging.getLogger(__name__)


class SyncData(ConsumerProcessorBase):
    """
    this consumer will forward logs to Splunk.
    """
    def __init__(self, target_setting=None):
        """
        """

        super(SyncData, self).__init__()   # remember to call base's init

        assert target_setting, ValueError("You need to configure settings of remote target")
        assert isinstance(target_setting, dict), ValueError("The settings should be dict to include necessary address and confidentials.")

        self.option = target_setting
        self.protocol = self.option['protocol']
        self.timeout = int(self.option.get('timeout', 120))
        self.sep = self.option.get('sep', "||")
        self.host = self.option["host"]
        self.port = int(self.option.get('port', 514))
        self.cert_path=self.option.get('cert_path', None)

        # try connection
        with SyslogClient(self.host, self.port, proto=self.protocol, timeout=self.timeout, cert_path=self.cert_path) as client:
            pass

    def process(self, log_groups, check_point_tracker):
        logs = PullLogResponse.loggroups_to_flattern_list(log_groups, time_as_str=True, decode_bytes=True)
        logger.info("Get data from shard {0}, log count: {1}".format(self.shard_id, len(logs)))

        try:
            with SyslogClient(self.host, self.port, proto=self.protocol, timeout=self.timeout, cert_path=self.cert_path) as client:
                for log in logs:
                    # Put your sync code here to send to remote.
                    # the format of log is just a dict with example as below (Note, all strings are unicode):
                    #    Python2: {"__time__": "12312312", "__topic__": "topic", u"field1": u"value1", u"field2": u"value2"}
                    #    Python3: {"__time__": "12312312", "__topic__": "topic", "field1": "value1", "field2": "value2"}
                    # suppose we only care about audit log
                    timestamp = datetime.fromtimestamp(int(log[u'__time__']))
                    del log['__time__']

                    io = six.StringIO()
                    first = True
                    for k, v in six.iteritems(log):
                        io.write("{0}{1}={2}".format(self.sep, k, v))

                    data = io.getvalue()
                    client.log(data, facility=self.option.get("facility", None), severity=self.option.get("severity", None), timestamp=timestamp, program=self.option.get("tag", None), hostname=self.option.get("hostname", None))

        except Exception as err:
            logger.debug("Failed to connect to remote syslog server ({0}). Exception: {1}".format(self.option, err))
            # TODO: add some error handling here or retry etc.
            raise err

        logger.info("Complete send data to remote")

        self.save_checkpoint(check_point_tracker)


def get_option():
    ##########################
    # Basic options
    ##########################

    # load connection info env and consumer group name from envs
    accessKeyId = os.environ.get('SLS_AK_ID', '')
    accessKey = os.environ.get('SLS_AK_KEY', '')
    endpoints = os.environ.get('SLS_ENDPOINTS', '').split(";")  # endpoints list separated by ;
    projects = os.environ.get('SLS_PROJECTS', '').split(";")    # projects list, separated by ; must be same len as endpoints
    logstores = os.environ.get('SLS_LOGSTORES', '').split(";")  # logstores list, seperated by ; pared with project. and separated by ',' for one project.
    consumer_group = os.environ.get('SLS_CG', '')

    assert endpoints and accessKeyId and accessKey and projects and logstores and consumer_group, \
        ValueError("endpoints/access_id/key/projects/logstores/consumer_group/name cannot be empty")

    assert len(endpoints) == len(projects) == len(logstores), ValueError("endpoints/projects/logstores must be paired")

    ##########################
    # Some advanced options
    ##########################

    # DON'T configure the consumer name especially when you need to run this program in parallel
    consumer_name = "{0}-{1}".format(consumer_group, current_process().pid)

    # This options is used for initialization, will be ignored once consumer group is created and each shard has beeen started to be consumed.
    # Could be "begin", "end", "specific time format in ISO", it's log receiving time.
    cursor_start_time = "begin"

    # once a client doesn't report to server * heartbeat_interval * 2 interval, server will consider it's offline and re-assign its task to another consumer.
    # thus  don't set the heatbeat interval too small when the network badwidth or performance of consumtion is not so good.
    heartbeat_interval = 60

    # if the coming data source data is not so frequent, please don't configure it too small (<1s)
    data_fetch_interval = 1
    
    executor = ThreadPoolExecutor(max_workers=4)

    options = []
    for i in range(len(endpoints)):
        endpoint = endpoints[i].strip()
        project = projects[i].strip()
        if not endpoint or not project:
            logger.error("project: {0} or endpoint {1} is empty, skip".format(project, endpoint))
            continue

        logstore_list = logstores[i].split(",")
        for logstore in logstore_list:
            logstore = logstore.strip()
            if not logstore:
                logger.error("logstore for project: {0} or endpoint {1} is empty, skip".format(project, endpoint))
                continue

            # create one consumer in the consumer group
            option = LogHubConfig(endpoint, accessKeyId, accessKey, project, logstore, consumer_group, consumer_name,
                                  cursor_position=CursorPosition.SPECIAL_TIMER_CURSOR,
                                  cursor_start_time=cursor_start_time,
                                  heartbeat_interval=heartbeat_interval,
                                  data_fetch_interval=data_fetch_interval)
            options.append(option)


    # syslog options
    settings = {
                "host": "220.100.162.15", # must
                "port": 514,       # must, port
                "protocol": "udp", # must, tcp, udp, tls (py3 only)
                "sep": "||",      # must, separator for key=value
                "cert_path": None,  # optional, cert path when TLS is configured
                "timeout": 120,   # optional, default 120
                "facility": syslogclient.FAC_USER,  # optional, default None means syslogclient.FAC_USER
                "severity": syslogclient.SEV_INFO,  # optional, default None means syslogclient.SEV_INFO
                "hostname": None, # optional, default hostname of local
                "tag": None # optional, tag for the log, default -
    }

    return executor, options, settings


def main():
    executor, options, settings = get_option()

    logger.info("*** start to consume data...")
    workers = []

    for option in options:
        worker = ConsumerWorker(SyncData, option, args=(settings,) )
        workers.append(worker)
        worker.start()

    try:
        for i, worker in enumerate(workers):
            while worker.is_alive():
                worker.join(timeout=60)
            logger.info("worker project: {0} logstore: {1} exit unexpected, try to shutdown it".format(
                options[i].project, options[i].logstore))
            worker.shutdown()
    except KeyboardInterrupt:
        logger.info("*** try to exit **** ")
        for worker in workers:
            worker.shutdown()

        # wait for all workers to shutdown before shutting down executor
        for worker in workers:
            while worker.is_alive():
                worker.join(timeout=60)

    executor.shutdown()


if __name__ == '__main__':
    main()
    
