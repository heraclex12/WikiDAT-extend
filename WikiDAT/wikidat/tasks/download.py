# -*- coding: utf-8 -*-
"""
Created on Thu Apr 10 18:02:16 2014

Download manager for dump files

@author: jfelipe
"""
from bs4 import BeautifulSoup
import multiprocessing as mp
import itertools
import requests
import re
import os
import sys
import hashlib
import logging
from utils import misc


class DumpIntegrityError(Exception):
    """Exception raised for errors in the input.

    Attributes:
        msg  -- explanation of the error
    """

    def __init__(self, file_path):
        self.file_path = file_path
        self.msg = ("""Dump file integrity error detected!\n File: {0}"""
                    .format(file_path))


class Downloader(object):
    """
    Download manager for any type of Wikipedia dump file
    Subclasses will instantiate methods to deal with the specific tasks
    to download each type of dump file.

    Dump files are retrieved from the selected mirror site. Currently
    http://dumps.wikimedia.your.org, is configured as the default option,
    as it allows two parallel downloading processes and it hosts up-to-date
    files from WMF original dump site.
    """

    def __init__(self, mirror="http://dumps.wikimedia.your.org/",
                 language='scowiki', dumps_dir=None):
        self.language = language
        self.mirror = mirror
        self.base_url = "".join([self.mirror, self.language])
        # print("Base URL is: %s" % (self.base_url))
        html_dates = requests.get(self.base_url)
        soup_dates = BeautifulSoup(html_dates.text, "lxml")

        # Get hyperlinks and timestamps of dumps for each available date
        # Ignore first line with link to parent folder
        self.dump_urldate = [link.get('href')
                             for link in soup_dates.find_all('a')][1:]
        self.dump_dates = [link.text
                           for link in soup_dates.find_all('td', 'm')][1:]
        # Stores re in subclass for type of dump file
        # To be filled in subclass with pattern for dump files
        self.match_pattern = ""
        if dumps_dir:
            self.dump_basedir = os.path.join("data", dumps_dir)
        else:
            self.dump_basedir = os.path.join("data", language + "_dumps")
        self.dump_paths = []  # List of paths to dumps in local filesystem
        self.md5_codes = {}  # Dict for md5 codes to verify dump files

    def download(self, dump_date=None):
        """
        Download all dump files for a given language in their own folder
        Return list of paths to dump files to be processed

        Default target dump_date is latest available dump (index -2, as the
        last item is the generic 'latest' date, not a real date)
        """
        if dump_date is None:
            dump_date = self.dump_urldate[-2]
        # Obtain content for dump summary page on requested date
        self.target_url = "".join([self.base_url, "/", dump_date])
        print("Target URL is: %s" % (self.target_url))
        html_dumps = requests.get(self.target_url)
        soup_dumps = BeautifulSoup(html_dumps.text, "lxml")

        # First of all, check that status of dump files is Done (ready)
        status_dumps = soup_dumps.find('p', class_='status').span.text
        if status_dumps != 'Dump complete':
            # TODO: Provide an alternative to the user (e.g. latest dump)
            print("Data dump for the selected date is not ready yet.")
            print("Please, provide a valid date for a completed dump process")
            print("or select the latest available dump")
            print("Program will exit now.")
            sys.exit()

        # Dump file(s) ready, proceed with list of files and download
        self.dump_urls = [link.get('href') for link in (soup_dumps.
                          find_all(href=re.compile(self.match_pattern)))]
        # Create directory for dump files if needed
        self.dump_dir = os.path.join(self.dump_basedir, dump_date)
        self.logs_dir = os.path.join(self.dump_basedir, dump_date, "logs")
        if not os.path.exists(self.dump_dir):
            os.makedirs(self.dump_dir)
        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)

        for url1, url2 in itertools.zip_longest(self.dump_urls[::2],
                                                self.dump_urls[1::2],
                                                fillvalue=None):
            file_name1 = url1.split('/')[-1]
            path_file1 = os.path.join(self.dump_dir, file_name1)
            self.dump_paths.append(path_file1)

            # Due to bandwith limitations in WMF mirror servers, you will not
            # be allowed to download more than 2 dump files at the same time
            proc_get1 = mp.Process(target=self._get_file,
                                   args=(url1, path_file1,))
            proc_get1.start()
            # Control here for even number of dumps (last element is None)
            if url2 is not None:
                file_name2 = url2.split('/')[-1]
                path_file2 = os.path.join(self.dump_dir, file_name2)
                self.dump_paths.append(path_file2)
                proc_get2 = mp.Process(target=self._get_file,
                                       args=(url2, path_file2,))
                proc_get2.start()
                proc_get2.join()

            # Wait until all downloads are finished
            proc_get1.join()

        print("Paths in download: ", str(self.dump_paths))
        # Verify integrity of downloaded dumps
        try:
            self._verify(self.target_url)
        except DumpIntegrityError as e:
            print(e.msg)

        print("File integrity checked, no errors found.")
        # Return list of paths to dumpfiles for data extraction
        return self.dump_paths, dump_date

    def _get_file(self, dump_url, path_file):
        """
        Retrieve individual dump file from dump_url and save it in dump_dir
        Progress bar taken from:
        http://stackoverflow.com/questions/15644964/
        python-progress-bar-and-downloads
        """
        local_dir = os.path.split(path_file)[0]
        file_name = os.path.split(path_file)[1]
        file_url = "".join([self.mirror, dump_url])
        print("File URL is: %s" % (file_url))

        # Setup log file
        log_file = os.path.join(local_dir, "logs", file_name + ".log")
        logging.basicConfig(filename=log_file, level=logging.INFO)

        resp_file = requests.get(file_url, stream=True)
        meta_file_size = float(resp_file.headers.get('content-length'))
        log_size_msg = "Downloading: {0} - [Size: {1}]"
        print(log_size_msg.format(file_name, misc.hfile_size(meta_file_size)))

        store_file = open(path_file, 'wb')
        part_len = 0
        completed = 0
        total_length = int(meta_file_size)
        logging.info(log_size_msg.format(file_name,
                                         misc.hfile_size(meta_file_size)))
        for data in resp_file.iter_content(chunk_size=65536):
            part_len += len(data)
            store_file.write(data)
            done = int(50 * part_len / total_length)
            if done > completed:
                # If there is progress to notify, log it
                completed = done
                logging.info("[%s%s] - [%3d %% completed]" % (
                             '=' * done,
                             ' ' * (50-done),
                             round(100 * part_len / total_length, 2))
                             )
        logging.info("File %s downloaded OK." % file_name)
        store_file.close()

    def _verify(self, target_url):
        """
        Verify integrity of downloaded dump files against MD5 checksums
        """
        html_dumps = requests.get(target_url)
        soup_dumps = BeautifulSoup(html_dumps.text, "lxml")
        md5_link = soup_dumps.find('p', class_='checksum').a['href']
        md5_url = "".join([self.mirror, md5_link])
        md5_codes = requests.get(md5_url).text
        md5_codes = md5_codes.split('\n')

        for fileitem in md5_codes:
            f = fileitem.split()
            if len(f) > 0:
                self.md5_codes[f[1]] = f[0]  # dict[fname] = md5code

        for path in self.dump_paths:
            filename = os.path.split(path)[1]  # Get filename from path
            with open(path, 'rb') as f:
                file_md5 = hashlib.md5(f.read()).hexdigest()
            original_md5 = self.md5_codes[filename]
            # TODO: Compare md5 hash of retrieved file with original
            if file_md5 != original_md5:
                # Raise error if they do not match
                raise DumpIntegrityError(path)


class RevHistDownloader(Downloader):
    """
    Downloads revision history files from selected mirror site.
    These are files with complete revision history information (all text)
    """

    def __init__(self, mirror, language, dumps_dir):
        super(RevHistDownloader, self).__init__(mirror=mirror,
                                                language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'pages-meta-history[\S]*\.xml[\S]*\.7z'


class RevMetaDownloader(Downloader):
    """
    Downloads revision meta files from selected mirror site.
    These are files with complete metadata for every revision (including
    rev_len, as stored in Wikipedia DB) but no revision text
    """
    def __init__(self, mirror, language, dumps_dir):
        super(RevMetaDownloader, self).__init__(mirror=mirror,
                                                language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'stub-meta-history[\d]*\.xml\.gz'


class LoggingDownloader(Downloader):
    """
    Download dump files for logging table, containing records of
    administrative and maintenance actions performed on pages and users
    """
    def __init__(self, mirror, language, dumps_dir):
        super(LoggingDownloader, self).__init__(mirror=mirror,
                                                language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'pages-logging[\d]*\.xml\.gz'


class UserGroupsDownloader(Downloader):
    """
    Download SQL dump with assignments of users to groups
    """
    def __init__(self, mirror, language, dumps_dir):
        super(UserGroupsDownloader, self).__init__(mirror=mirror,
                                                   language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'user_groups[\d]*\.sql\.gz'


class IWLinksDownloader(Downloader):
    """
    Download SQL dump with interwiki link tracking records
    """
    def __init__(self, mirror, language, dumps_dir):
        super(IWLinksDownloader, self).__init__(mirror=mirror,
                                                language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'iwlinks[\d]*\.sql\.gz'


class TemplateLinksDownloader(Downloader):
    """
    Download SQL dump with interwiki prefixes and links for this Wikipedia
    language
    """
    def __init__(self, mirror, language, dumps_dir):
        super(TemplateLinksDownloader, self).__init__(mirror=mirror,
                                                      language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'templatelinks[\d]*\.sql\.gz'


class PageRestrDownloader(Downloader):
    """
    Download SQL dump with current page restrictions
    """
    def __init__(self, mirror, language, dumps_dir):
        super(PageRestrDownloader, self).__init__(mirror=mirror,
                                                  language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'page_restrictions[\d]*\.sql\.gz'


class CategoryDownloader(Downloader):
    """
    Download SQL dump with category information
    """
    def __init__(self, mirror, language, dumps_dir):
        super(CategoryDownloader, self).__init__(mirror=mirror,
                                                 language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'category[\d]*\.sql\.gz'


class CatLinksDownloader(Downloader):
    """
    Download SQL dump with category membership links for every page
    """
    def __init__(self, mirror, language, dumps_dir):
        super(CatLinksDownloader, self).__init__(mirror=mirror,
                                                 language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'categorylinks[\d]*\.sql\.gz'


class LangLinksDownloader(Downloader):
    """
    Download SQL dump with interlanguage link records
    """
    def __init__(self, mirror, language, dumps_dir):
        super(LangLinksDownloader, self).__init__(mirror=mirror,
                                                  language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'langlinks[\d]*\.sql\.gz'


class ExtLinksDownloader(Downloader):
    """
    Download SQL dump with external URL link records
    """
    def __init__(self, mirror, language, dumps_dir):
        super(ExtLinksDownloader, self).__init__(mirror=mirror,
                                                 language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'externallinks[\d]*\.sql\.gz'


class PagesLinksDownloader(Downloader):
    """
    Download SQL dump with wiki page-to-page link records
    """
    def __init__(self, mirror, language, dumps_dir):
        super(PagesLinksDownloader, self).__init__(mirror=mirror,
                                                        language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'pagelinks[\d]*\.sql\.gz'


class ImageLinksDownloader(Downloader):
    """
    Download SQL dump with media and file usage information
    """
    def __init__(self, mirror, language, dumps_dir):
        super(ImageLinksDownloader, self).__init__(mirror=mirror,
                                                   language=language)
        # Customized pattern to find dump files on mirror server page
        self.match_pattern = 'imagelinks[\d]*\.sql\.gz'
