# -*- coding: utf-8 -*-
"""
Created on Sun Mar 30 18:09:16 2014

@author: jfelipe
"""
# import multiprocessing as mp
import sys
import os
import time
import multiprocessing as mp
import subprocess
from .processors import Producer, Processor, Consumer
from .dump import DumpFile, process_xml
from .page import pages_to_file, pages_file_to_db
from .revision import revs_to_file, revs_file_to_db
from .logitem import logitem_to_file, logitem_file_to_db
from utils.dbutils import MySQLDB
from elasticsearch import Elasticsearch, helpers


class ETL(mp.Process):
    """
    Abstract class defining common behaviour for all ETL (Extraction,
    Tranformation and Load) workflows with Wikipedia data
    """

    def __init__(self, group=None, target=None, name=None, args=None,
                 kwargs=None, lang=None, db_name=None, db_user=None,
                 db_passw=None):
        """
        Initialize new worfklow
        """
        super(ETL, self).__init__(name=name)
        self.target = target
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}

        self.lang = lang
        self.db_name = db_name
        self.db_user = db_user
        self.db_passw = db_passw


class RevisionHistoryETL(ETL):
    """
    Models workflow to import page and revision history data from Wikipedia
    database dump files
    """
    def __init__(self, group=None, target=None, name=None, args=None,
                 kwargs=None, paths_queue=None, lang=None, page_fan=1,
                 rev_fan=3, page_cache_size=1000000, rev_cache_size=1000000,
                 db_name=None, db_user=None, db_passw=None,
                 base_port=None, control_port=None):
        """
        Initialize new PageRevision workflow
        """
        super(RevisionHistoryETL,
              self).__init__(group=None, target=None, name=name, args=None,
                             kwargs=None, lang=lang, db_name=db_name,
                             db_user=db_user, db_passw=db_passw)
        self.page_fan = page_fan
        self.rev_fan = rev_fan
        self.paths_queue = paths_queue
        self.page_cache_size = page_cache_size
        self.rev_cache_size = rev_cache_size
        self.base_port = base_port
        self.control_port = control_port

    def run(self):
        """
        Execute workflow to import revision history data from dump files

        The data loading workflow is composed of a number of processor
        elements, which can be:

            - Producer (P): raw input data --> input element queue
            - ConsumerProducer (CP): input element queue --> insert db queue
            - Consumer (C): insert db queue --> database (MySQL/MariaDB)

        In this case, the logical combination is usually N:N:1 (P, CP, C)
        """
        start = time.time()
        print(self.name, "Starting PageRevisionETL workflow at %s" % (
                         time.strftime("%Y-%m-%d %H:%M:%S %Z",
                                       time.localtime())))

        db_ns = MySQLDB(host='localhost', port=5306, user=self.db_user,
                        passwd=self.db_passw, db=self.db_name)
        db_ns.connect()

        db_pages = MySQLDB(host='localhost', port=5306,
                           user=self.db_user, passwd=self.db_passw,
                           db=self.db_name)
        db_pages.connect()

        db_revs = MySQLDB(host='localhost', port=5306, user=self.db_user,
                          passwd=self.db_passw, db=self.db_name)

        es_revs = Elasticsearch(['10.30.78.22'])

        db_revs.connect()

        # DATA EXTRACTION
        # Use consistent naming for all child processes
        xml_reader_name = '-'.join([self.name, 'xml_reader'])
        page_proc_name = '-'.join([self.name, 'process_page'])
        rev_proc_name = '-'.join([self.name, 'process_revision'])
        page_insert_name = '-'.join([self.name, 'insert_page'])
        rev_insert_name = '-'.join([self.name, 'insert_revision'])

        for path in iter(self.paths_queue.get, 'STOP'):
            # Start subprocess to extract elements from revision dump file
            dump_file = DumpFile(path)
            xml_reader = Producer(name=xml_reader_name,
                                  target=process_xml,
                                  kwargs=dict(
                                      dump_file=dump_file),
                                  consumers=self.page_fan + self.rev_fan,
                                  push_pages_port=self.base_port,
                                  push_revs_port=self.base_port+1,
                                  control_port=self.control_port)
            xml_reader.start()
            print(xml_reader_name, "started")
            print(self.name, "Extracting data from XML revision history file:")
            print(path)

            # List to keep tracking of page and revision workers
            workers = []
            db_workers_revs = []
            # Create and start page processes
            for worker in range(self.page_fan):
                page_worker_name = '-'.join([page_proc_name, str(worker)])
                process_page = Processor(name=page_worker_name,
                                         target=pages_to_file,
                                         producers=1, consumers=1,
                                         pull_port=self.base_port,
                                         push_port=self.base_port+2,
                                         control_port=self.control_port)
                process_page.start()
                workers.append(process_page)
                print(page_worker_name, "started")

            # Create and start revision processes
            for worker in range(self.rev_fan):
                rev_worker_name = '-'.join([rev_proc_name, str(worker)])

                db_wrev = MySQLDB(host='localhost', port=5306,
                                  user=self.db_user,
                                  passwd=self.db_passw, db=self.db_name)
                db_wrev.connect()

                process_revision = Processor(name=rev_worker_name,
                                             target=revs_to_file,
                                             kwargs=dict(
                                                 lang=self.lang),
                                             producers=1, consumers=1,
                                             pull_port=self.base_port+1,
                                             push_port=self.base_port+3,
                                             control_port=self.control_port)
                process_revision.start()
                workers.append(process_revision)
                db_workers_revs.append(db_wrev)
                print(rev_worker_name, "started")

            # Create directory for logging files if it does not exist
            log_dir = os.path.join(os.path.split(path)[0], 'logs')
            tmp_dir = os.path.join(os.getcwd(), os.path.split(path)[0], 'tmp')
            file_name = os.path.split(path)[1]

            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)
            log_file = os.path.join(log_dir, file_name + '.log')

            page_insert_db = Consumer(name=page_insert_name,
                                      target=pages_file_to_db,
                                      kwargs=dict(con=db_pages,
                                                  log_file=log_file,
                                                  tmp_dir=tmp_dir,
                                                  file_rows=self.page_cache_size,
                                                  etl_prefix=self.name),
                                      producers=self.page_fan,
                                      pull_port=self.base_port+2)

            rev_insert_db = Consumer(name=rev_insert_name,
                                     target=revs_file_to_db,
                                     kwargs=dict(con=db_revs,
                                                 es_con=es_revs,
                                                 log_file=log_file,
                                                 tmp_dir=tmp_dir,
                                                 file_rows=self.rev_cache_size,
                                                 etl_prefix=self.name),
                                     producers=self.rev_fan,
                                     pull_port=self.base_port+3)

            page_insert_db.start()
            print(page_insert_name, "started")
            rev_insert_db.start()
            print(rev_insert_name, "started")

            print(self.name, "Waiting for all processes to finish...")
            print()
            xml_reader.join()
            for w in workers:
                w.join()
            page_insert_db.join()
            rev_insert_db.join()

            # Mark this path as done
            self.paths_queue.task_done()

        # Mark STOP message as processed and finish
        self.paths_queue.task_done()

        end = time.time()
        print(self.name, ": All tasks done in %.4f sec." % ((end-start)/1.))
        print()
        db_ns.close()
        db_pages.close()
        db_revs.close()
        for dbcon in db_workers_revs:
            dbcon.close()


class RevisionMetaETL(ETL):
    """
    Implements workflow to extract and store metadata for pages and
    revisions (stub-meta-history.xml files)
    """
    pass


class LoggingETL(ETL):
    """
    Implements workflow to extract and store information from logged
    actions in MediaWiki. For instance, user blocks, page protections,
    new users, flagged revisions reviews, etc.
    """
    def __init__(self, group=None, target=None, name=None, args=None,
                 kwargs=None, path=None, lang=None, log_fan=1,
                 log_cache_size=1000000,
                 db_name=None, db_user=None, db_passw=None,
                 base_port=None, control_port=None):
        """
        Initialize new PageRevision workflow
        """
        super(LoggingETL,
              self).__init__(group=None, target=None, name=name, args=None,
                             kwargs=None, lang=lang, db_name=db_name,
                             db_user=db_user, db_passw=db_passw)

        self.path = path
        self.log_fan = log_fan
        self.log_cache_size = log_cache_size
        self.base_port = base_port
        self.control_port = control_port

    def run(self):
        """
        Execute workflow to import logging records of actions on pages and
        users from dump file

        The data loading workflow is composed of a number of processor
        elements, which can be:

            - Producer (P): raw input data --> input element queue
            - ConsumerProducer (CP): input element queue --> insert db queue
            - Consumer (C): insert db queue --> database (MySQL/MariaDB)

        In this case, the usual combination is 1:N:1 (P, CP, C)
        """
        start = time.time()
        print("Starting LoggingETL workflow at %s" % (
              time.strftime("%Y-%m-%d %H:%M:%S %Z",
                            time.localtime())))

        # DATA EXTRACTION
        xml_reader_name = '-'.join([self.name, 'xml_reader'])
        logitem_proc_name = '-'.join([self.name, 'process_logitem'])
        logitem_insert_name = '-'.join([self.name, 'insert_logitem'])
        # Start subprocess to extract elements from logging dump file
        file_path = self.path[0]
        dump_file = DumpFile(file_path)
        xml_reader = Producer(name=xml_reader_name,
                              target=process_xml,
                              kwargs=dict(
                                  dump_file=dump_file),
                              consumers=self.log_fan,
                              push_logs_port=self.base_port,
                              control_port=self.control_port)
        xml_reader.start()
        print(xml_reader_name, "started")
        print(self.name, "Extracting data from XML revision history file:")
        print(str(self.path[0]))

        # List to keep tracking of logitem workers
        workers = []
        # Create and start page processes
        for worker in range(self.log_fan):
            worker_name = '-'.join([logitem_proc_name, str(worker)])
            process_logitems = Processor(name=worker_name,
                                         target=logitem_to_file,
                                         producers=1, consumers=1,
                                         pull_port=self.base_port,
                                         push_port=self.base_port+2,
                                         control_port=self.control_port)
            process_logitems.start()
            workers.append(process_logitems)
            print(worker_name, "started")

        # Create directory for logging files if it does not exist
        log_dir = os.path.join(os.path.split(file_path)[0], 'logs')
        tmp_dir = os.path.join(os.getcwd(), os.path.split(file_path)[0], 'tmp')
        file_name = os.path.split(file_path)[1]

        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir)
        log_file = os.path.join(log_dir, file_name + '.log')

        db_log = MySQLDB(host='localhost', port=5306, user=self.db_user,
                         passwd=self.db_passw, db=self.db_name)
        db_log.connect()
        logitem_insert_db = Consumer(name=logitem_insert_name,
                                     target=logitem_file_to_db,
                                     kwargs=dict(con=db_log,
                                                 log_file=log_file,
                                                 tmp_dir=tmp_dir,
                                                 file_rows=self.log_cache_size,
                                                 etl_prefix=self.name),
                                     producers=self.log_fan,
                                     pull_port=self.base_port+2)

        print(logitem_insert_name, "started")
        logitem_insert_db.start()

        print("Waiting for all processes to finish...")
        print()
        xml_reader.join()
        for w in workers:
            w.join()
        logitem_insert_db.join()

        # All operations finished
        end = time.time()
        print("All tasks done in %.4f sec." % ((end-start)/1.))
        print()
        db_log.close()


class SQLDumpsETL(ETL):
    """
    Implements workflow to load native SQL dump files, created with
    mysqldump and published in compressed format (gzip file)
    """
    def __init__(self, group=None, target=None, name=None, args=None,
                 kwargs=None, path=None, lang=None,
                 db_name=None, db_user=None, db_passw=None):
        """
        Initialize new PageRevision workflow
        """
        super(SQLDumpsETL,
              self).__init__(group=None, target=None, name=name, args=None,
                             kwargs=None, lang=lang, db_name=db_name,
                             db_user=db_user, db_passw=db_passw)
        self.path = path

    def run(self):
        """
        Docstring
        """
        # TODO: Create Popen o similar subprocessing strategy w/ shell
        # gzip -cd file | mysql [params]
        # or in case the file is already uncompressed
        # cat sql | mysql [params]
        for path in self.path:
            if '.gz' in path:
                command = "gzip -cd {0} | mysql -u {1} -p{2} {3}"
            else:
                command = "cat {0} | mysql -u {1} -p{2} {3}"
            print("Processing file ", os.path.split(path)[1])
            p = subprocess.Popen(command.format(path, self.db_user,
                                                self.db_passw, self.db_name),
                                 shell=True,
                                 stdout=subprocess.PIPE,
                                 stderr=open(os.devnull, "w")
                                 )
        # sys.stderr.write(p.stdout.read(1000))
        # return False
        return p.stdout


if __name__ == '__main__':
    path = sys.argv[1]
    page_fan = int(sys.argv[2])
    rev_fan = int(sys.argv[3])
    lang = sys.argv[4]
    db_name = sys.argv[5]
    db_user = sys.argv[6]
    db_passw = sys.argv[7]

    workflow = RevisionHistoryETL(path, page_fan, rev_fan, lang, db_name,
                                  db_user, db_passw)
    workflow.run()
