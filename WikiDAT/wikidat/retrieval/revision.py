# -*- coding: utf-8 -*-
"""
Created on Sat Mar 29 22:13:42 2014

@author: jfelipe
"""
import hashlib
import time
from utils import maps
from .data_item import DataItem
import csv
import os
import redis
import ipaddress
import logging
import json
from elasticsearch import Elasticsearch, helpers
from wikiextractor.wikiextractor.clean import clean_markup


class Revision(DataItem):
    """
    Models Revision elements in Wikipedia dump files
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor method for Revision objects. Must forward params to
        parent class DataItem (mandatory inheritance)

        The following keys must be populated:
        ---------
        * id: Unique numeric identifier for this revision
        * rev_user: Numeric identifier of author of this revision (0 is anon)
        * rev_timestamp: Timestamp when this rev was saved in database
        * rev_len: Length in bytes of this revision
        * ... (other params)
        """
        super(Revision, self).__init__(*args, **kwargs)


def process_revs(rev_iter, con=None, lang=None):
    """
    Process iterator of Revision objects extracted from dump files
    :Parameters:
        - rev_iter: iterator of Revision objects
        - lang: identifier of Wikipedia language edition from which this
        element comes from (e.g. frwiki, eswiki, dewiki...)
    """
    # Get tags to identify Featured Articles, Featured Lists and
    # Good Articles

    if ((lang in maps.FA_RE) and (lang in maps.FLIST_RE) and
            (lang in maps.GA_RE)):
        fa_pat = maps.FA_RE[lang]
        flist_pat = maps.FLIST_RE[lang]
        ga_pat = maps.GA_RE[lang]
    else:
        raise RuntimeError('Unsupported language ' + lang)

    for rev in rev_iter:
        contrib_dict = rev['contrib_dict']

        # ### TEXT-RELATED OPERATIONS ###
        # Calculate SHA-256 hash, length of revision text and check
        # for REDIRECT
        # TODO: Inspect why there are pages without text

        # Default values to 0. These fields will be set below if any of the
        # target patterns is detected
        rev['redirect'] = '0'
        rev['is_fa'] = '0'
        rev['is_flist'] = '0'
        rev['is_ga'] = '0'

        if rev['text'] is not None:
            text = clean_markup(rev['text'])
            text_hash = text
            rev['len_text'] = str(len(text))

            # Detect pattern for redirect pages
            if rev['text'][0:9].upper() == '#REDIRECT':
                rev['redirect'] = '1'

            # FA and FList detection
            # Currently 39 languages are supported regarding FA detection
            # We only enter pattern matching for revisions of pages in
            # main namespace
            if rev['ns'] == '0':
                if fa_pat is not None:
                    mfa = fa_pat.search(rev['text'])
                    # Case of standard language, one type of FA template
                    if (mfa is not None and len(mfa.groups()) == 1):
                        rev['is_fa'] = '1'
                    # Case of fawiki or cawiki, 2 types of FA templates
                    # Possible matches: (A, None) or (None, B)
                    if lang == 'fawiki' or lang == 'cawiki':
                        if (mfa is not None and len(mfa.groups()) == 2 and
                                (mfa.groups()[1] is None or
                                 mfa.groups()[0] is None)):
                                    rev['is_fa'] = '1'

                # Check if FLIST is supported in this language, detect if so
                if flist_pat is not None:
                    mflist = flist_pat.search(rev['text'])
                    if mflist is not None and len(mflist.groups()) == 1:
                        rev['is_flist'] = '1'

                # Check if GA is supported in this language, detect if so
                if ga_pat is not None:
                    mga = ga_pat.search(rev['text'])
                    if mga is not None and len(mga.groups()) == 1:
                        rev['is_ga'] = '1'
        # Compute hash for empty text here instead of in default block above
        # This way, we avoid computing the hash twice for revisions with text
        else:
            rev['len_text'] = '0'
            text_hash = ''

        # SQL query building
        # Build extended inserts for people, revision and revision_hash
        rev_insert = "".join(["(", rev['id'], ",",
                              rev['page_id'], ","])

        rev_hash = "".join(["(", rev['id'], ",",
                            rev['page_id'], ","])

        # USER ID PROCESSING
        # Check if revision has a valid contributor, update contrib_dict
        # accordingly
        if len(contrib_dict) > 0:
            # Anonymous user
            if 'ip' in contrib_dict:
                # new_user = False
                rev_insert = "".join([rev_insert, "0,"])
                rev_hash = "".join([rev_hash, "0,"])

                # TODO: Upload IP info to people table

            # Registered user
            else:
                rev_insert = "".join([rev_insert,
                                      contrib_dict['id'], ","])
                rev_hash = "".join([rev_hash,
                                    contrib_dict['id'], ","])
                query_user = "".join(["SELECT rev_user, ",
                                      "rev_user_text ",
                                      "FROM people WHERE rev_user = ",
                                      contrib_dict['id']])
                # If this is a new user add info to people table
                known_user = con.execute_query(query_user)
                if known_user is None:
                    # Standard case of new username
                    if contrib_dict['username'] is not None:
                        # Activate flag to insert new user info in DB
                        # new_user = True
                        # Update common users cache
                        user_insert = "".join(["(",
                                               contrib_dict['id'],
                                               ",'",
                                               contrib_dict['username'].
                                               replace("\\", "\\\\").
                                               replace("'", "\\'").
                                               replace('"', '\\"'),
                                               "')"])
                        con.send_query("".join(["INSERT INTO people ",
                                                "VALUES",
                                                user_insert])
                                       )

                    # Handle strange case of new user w/o username
                    else:
                        user_insert = "".join(["(", contrib_dict['id'],
                                               "NULL)"])
                        con.send_query("".join(["INSERT INTO people ",
                                                "VALUES",
                                                user_insert]),
                                       )
                        # new_user = False

                else:
                    # Case of previously unknown user
                    if known_user[0][1] is None and\
                            contrib_dict['username'] is not None:
                        # new_user = True

                        update_login = "".join(["UPDATE people SET ",
                                                "rev_user_text = '",
                                                contrib_dict['username'].
                                                replace("\\", "\\\\").
                                                replace("'", "\\'").
                                                replace('"', '\\"'), "' ",
                                                "WHERE rev_user=",
                                                contrib_dict['username']])
                        con.send_query(update_login)
                    # else:
                        # new_user = False

        # TODO: Inspect why there are revisions without contributor
        # Mark revision as missing contributor
        else:
            # new_user = False
            rev_insert = "".join([rev_insert, "-1, "])
            rev_hash = "".join([rev_hash, "-1, "])

        # TIMESTAMP PROCESSING
        # rev_timestamp
        rev['timestamp'] = rev['timestamp'].\
            replace('Z', '').replace('T', ' ')
        rev_insert = "".join([rev_insert, "'",
                              rev['timestamp'], "',"])
        # rev_len
        rev_insert = "".join([rev_insert,
                              rev['len_text'], ","])
        # rev_parent_id
        if rev['rev_parent_id'] is not None:
            rev_insert = "".join([rev_insert,
                                  rev['rev_parent_id'], ","])
        else:
            rev_insert = "".join([rev_insert, "NULL,"])

        # rev_redirect
        rev_insert = "".join([rev_insert,
                              rev['redirect'], ","])

        if 'minor' in rev:
            rev_insert = "".join([rev_insert, "0,"])
        else:
            rev_insert = "".join([rev_insert, "1,"])

        # Add is_fa and is_flist fields
        rev_insert = "".join([rev_insert,
                              rev['is_fa'], ",",
                              rev['is_flist'], ",",
                              rev['is_ga'], ","])

        if 'comment' in rev and\
                rev['comment'] is not None:
            rev_insert = "".join([rev_insert, '"',
                                  rev['comment'].
                                  replace("\\", "\\\\").
                                  replace("'", "\\'").
                                  replace('"', '\\"'), '")'])
        else:
            rev_insert = "".join([rev_insert, "'')"])

        # Finish insert for revision_hash
        rev_hash = "".join([rev_hash,
                            "'", text_hash, "')"])

        yield rev_hash

        rev = None
        contrib_dict = None
        text = None
        text_hash = None


def revs_to_file(rev_iter, lang=None):
    """
    Process iterator of Revision objects extracted from dump files
    :Parameters:
        - rev_iter: iterator of Revision objects
        - lang: identifier of Wikipedia language edition from which this
        element comes from (e.g. frwiki, eswiki, dewiki...)
    """
    # Initialize connections to Redis DBs
    redis_cache = redis.Redis(host='localhost')

    # Get tags to identify Featured Articles, Featured Lists and
    # Good Articles
    if ((lang in maps.FA_RE) and (lang in maps.FLIST_RE) and
            (lang in maps.GA_RE)):
        fa_pat = maps.FA_RE[lang]
        flist_pat = maps.FLIST_RE[lang]
        ga_pat = maps.GA_RE[lang]
    else:
        raise RuntimeError('Unsupported language ' + lang)

    for rev in rev_iter:
        contrib_dict = rev['contrib_dict']

        # ### TEXT-RELATED OPERATIONS ###
        # Calculate SHA-256 hash, length of revision text and check
        # for REDIRECT
        # TODO: Inspect why there are pages without text

        # Default values to 0. These fields will be set below if any of the
        # target patterns is detected
        rev['redirect'] = '0'
        rev['is_fa'] = '0'
        rev['is_flist'] = '0'
        rev['is_ga'] = '0'

        if rev['text'] is not None:
            text = clean_markup(rev['text'])
            text_hash = text
            rev['len_text'] = str(len(text))

            # Detect pattern for redirect pages
            if rev['text'][0:9].upper() == '#REDIRECT':
                rev['redirect'] = '1'

            # FA and FList detection
            # Currently 39 languages are supported regarding FA detection
            # We only enter pattern matching for revisions of pages in
            # main namespace
            if rev['ns'] == '0':
                if fa_pat is not None:
                    mfa = fa_pat.search(rev['text'])
                    # Case of standard language, one type of FA template
                    if (mfa is not None and len(mfa.groups()) == 1):
                        rev['is_fa'] = '1'
                    # Case of fawiki or cawiki, 2 types of FA templates
                    # Possible matches: (A, None) or (None, B)
                    if lang == 'fawiki' or lang == 'cawiki':
                        if (mfa is not None and len(mfa.groups()) == 2 and
                                (mfa.groups()[1] is None or
                                 mfa.groups()[0] is None)):
                                    rev['is_fa'] = '1'

                # Check if FLIST is supported in this language, detect if so
                if flist_pat is not None:
                    mflist = flist_pat.search(rev['text'])
                    if mflist is not None and len(mflist.groups()) == 1:
                        rev['is_flist'] = '1'

                # Check if GA is supported in this language, detect if so
                if ga_pat is not None:
                    mga = ga_pat.search(rev['text'])
                    if mga is not None and len(mga.groups()) == 1:
                        rev['is_ga'] = '1'
        # Compute hash for empty text here instead of in default block above
        # This way, we avoid computing the hash twice for revisions with text
        else:
            rev['len_text'] = '0'
            text_hash = ''

        # USER PROCESSING
        # Case of known user
        if len(contrib_dict) > 0:
            # Anonymous user
            if 'ip' in contrib_dict:
                user = 0
                ip = str(contrib_dict['ip'])
                redis_cache.hset(lang + ':revsanon', int(rev['id']),
                                 int(ipaddress.ip_address(ip)))
            # Registered user
            else:
                user = int(contrib_dict['id'])
                username = contrib_dict['username']
                # Case of missing user id but w/ username
                # The username is probably invalid now,
                # insert in separate table
                if user == 0:
                    user = -2  # Special value for case: (NULL, username)
                    redis_cache.hset(lang + ':userzero', int(rev['id']),
                                     username)
                # Username is known
                if username is not None:
                    redis_cache.hset(lang + ':users', user, username)
                # Handle strange cases of user ID w/o username
                else:
                    stored_name = redis_cache.hget(lang + ':users', user)
                    # If user is not known, then insert entry w/o username
                    # Otherwise, skip and wait for other entry w/ username
                    if not stored_name:
                        redis_cache.hset(lang + ':users', user, '')
        # Case of unknown user: neither user_id nor user_name
        else:
            user = -1  # Special value

        # Tuple of revision values
        # rev_insert = (int(rev['id']), int(rev['page_id']), int(user),
        #               rev['timestamp'].replace('Z', '').replace('T', ' '),
        #               int(rev['len_text']),
        #               (int(rev['rev_parent_id'])
        #                if rev['rev_parent_id'] is not None else u'NULL'),
        #               int(rev['redirect']),
        #               (0 if 'minor' in rev else 1),
        #               int(rev['is_fa']), int(rev['is_flist']),
        #               int(rev['is_ga']),
        #               (rev['comment'] if 'comment' in rev and
        #                rev['comment'] is not None else u'NULL'),
        #               )

        # dict of revision_hash values
        if int(rev['redirect']) == 0:
            rev_hash = {
                '_id': int(rev['id']),
                'timestamp': rev['timestamp'].replace('Z', '').replace('T', ' '),
                'parent_id': (int(rev['rev_parent_id']) if rev['rev_parent_id'] is not None else -1),
                'page_id':  int(rev['page_id']),
                'comment': (rev['comment'] if 'comment' in rev and
                            rev['comment'] is not None else u'NULL'),
                'content':   text_hash,
                        }

            yield rev_hash

        rev = None
        contrib_dict = None
        text = None
        text_hash = None
        # TODO: Handle disconnection of clients from Redis server??


def revs_file_to_db(rev_iter, con=None, es_con=None, log_file=None,
                    tmp_dir=None, file_rows=1000000, etl_prefix=None):
    """
    Processor to insert revision info in DB

    This version uses an intermediate temp data file to speed up bulk data
    loading in MySQL/MariaDB, using LOAD DATA INFILE.

    Arguments:
        - rev_iter: Iterator providing tuples (rev_insert, rev_hash_insert)
        - con: Connection to local DB
        - log_file: Log file to track progress of data loading operations
        - tmp_dir: Directory to store temporary data files
        - file_rows: Number of rows to store in each tmp file
        - etl_prefix: Identifies the ETL process for this worker
    """
    insert_rows = 0
    total_revs = 0

    logging.basicConfig(filename=log_file, level=logging.DEBUG)
    print("Starting revision data loading at %s." % (
        time.strftime("%Y-%m-%d %H:%M:%S %Z",
                      time.localtime())))
    logging.info("Starting revision data loading at %s." % (
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))

    # LOAD REVISION DATA
    insert_rev = """LOAD DATA LOCAL INFILE '%s' INTO TABLE revision
                    FIELDS OPTIONALLY ENCLOSED BY '"'
                    TERMINATED BY '\t' ESCAPED BY '"'
                    LINES TERMINATED BY '\n'"""

    insert_rev_hash = """LOAD DATA LOCAL INFILE '%s' INTO TABLE revision_hash
                         FIELDS OPTIONALLY ENCLOSED BY '"'
                         TERMINATED BY '\t' ESCAPED BY '"'
                         LINES TERMINATED BY '\n'"""

    path_file_rev = os.path.join(tmp_dir, etl_prefix + '_revision.csv')
    path_file_rev_hash = os.path.join(tmp_dir,
                                      etl_prefix + '_revision_hash.csv')

    # Delete previous versions of tmp files if present
    if os.path.isfile(path_file_rev):
        os.remove(path_file_rev)
    if os.path.isfile(path_file_rev_hash):
        os.remove(path_file_rev_hash)

    for rev_hash in rev_iter:
        total_revs += 1

        # Initialize new temp data file
        if insert_rows == 0:
            rev_his = []
            # file_rev = open(path_file_rev, 'w')
            # file_rev_hash = open(path_file_rev_hash, 'w')
            # writer = csv.writer(file_rev, dialect='excel-tab',
            #                     lineterminator='\n')
            # writer2 = csv.writer(file_rev_hash, dialect='excel-tab',
            #                      lineterminator='\n')

        # Write data to tmp file
        try:
            rev_his.append(rev_hash)
            # writer.writerow([s if isinstance(s, str)
            #                  else str(s) for s in rev])
            #
            # writer2.writerow([s if isinstance(s, str)
            #                   else str(s) for s in rev_hash])
        except Exception as e:
            print("Error writing CSV files with revision info...")
            print(e)

        insert_rows += 1

        # Call MySQL to load data from file and reset rows counter
        if insert_rows == file_rows:
            # file_rev.close()
            # file_rev_hash.close()
            # con.send_query(insert_rev % path_file_rev)
            # con.send_query(insert_rev_hash % path_file_rev_hash)
            helpers.bulk(es_con, rev_his, index='viwiki_history')

            logging.info("%s revisions %s." % (
                         total_revs,
                         time.strftime("%Y-%m-%d %H:%M:%S %Z",
                                       time.localtime())))
            # Reset row counter
            insert_rows = 0
            # No need to delete tmp files, as they are empty each time we
            # open them again for writing

    # Load remaining entries in last tmp files into DB
    # file_rev.close()
    # file_rev_hash.close()

    # con.send_query(insert_rev % path_file_rev)
    # con.send_query(insert_rev_hash % path_file_rev_hash)
    helpers.bulk(es_con, rev_his, index='viwiki_history')
    # TODO: Clean tmp files, uncomment the following lines
#    os.remove(path_file_rev)
#    os.remove(path_file_rev_hash)

    # Log end of tasks and exit
    logging.info("COMPLETED: %s revisions processed %s." % (
                 total_revs,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))


def users_file_to_db(con=None, lang=None, log_file=None, tmp_dir=None):
    """
    Processor to insert revision info in DB

    This version uses an intermediate temp data file to speed up bulk data
    loading in MySQL/MariaDB, using LOAD DATA INFILE.

    Arguments:
        - con: Connection to local DB
        - log_file: Log file to track progress of data loading operations
        - tmp_dir: Directory to store temporary data files
    """
    logging.basicConfig(filename=log_file, level=logging.DEBUG)
    # Initialize connections to Redis DBs
    redis_cache = redis.Redis(host='localhost', decode_responses=True)

    # Add special values to hash 'users'
    redis_cache.hset(lang + ':users', 0, 'Anonymous user')
    redis_cache.hset(lang + ':users', -1, 'NA')
    redis_cache.hset(lang + ':users', -2, 'Missing ID')

    # LOAD USERS DATA
    # Load user info from Redis cache into persistent DB storage
    insert_anons = """LOAD DATA LOCAL INFILE '%s' INTO TABLE revision_IP
                      FIELDS OPTIONALLY ENCLOSED BY '"'
                      TERMINATED BY '\t' ESCAPED BY '"'
                      LINES TERMINATED BY '\n'"""

    insert_users = """LOAD DATA LOCAL INFILE '%s' INTO TABLE user
                      FIELDS OPTIONALLY ENCLOSED BY '"'
                      TERMINATED BY '\t' ESCAPED BY '"'
                      LINES TERMINATED BY '\n'"""

    insert_users_zero = """LOAD DATA LOCAL INFILE '%s' INTO TABLE revision_user_zero
                           FIELDS OPTIONALLY ENCLOSED BY '"'
                           TERMINATED BY '\t' ESCAPED BY '"'
                           LINES TERMINATED BY '\n'"""
    # Anonymous IPs
    path_file_anons = os.path.join(tmp_dir, lang + '_anon_IPs.csv')
    # Delete previous versions of tmp files if present
    if os.path.isfile(path_file_anons):
        os.remove(path_file_anons)

    file_anons = open(path_file_anons, 'w')
    writer_anons = csv.writer(file_anons, dialect='excel-tab',
                              lineterminator='\n')

    list_anons = []
    for rev_anon in redis_cache.hscan_iter(lang + ':revsanon', count=1000):
        list_anons.append(rev_anon)  # Makes sure we do not have duplicates

    total_anons = len(list_anons)

    for rev_anon in list_anons:  # Save list of anonymous revs to tmp file
        try:
            writer_anons.writerow([s for s in rev_anon])
        except Exception as e:
            print("Error writing CSV file for anonymous users...")
            print(e)
    file_anons.close()
    del list_anons

    # Registered users
    path_file_users = os.path.join(tmp_dir, lang + '_users.csv')
    # Delete previous versions of tmp files if present
    if os.path.isfile(path_file_users):
        os.remove(path_file_users)
    file_users = open(path_file_users, 'w')
    writer_users = csv.writer(file_users, dialect='excel-tab',
                              lineterminator='\n')

    list_users = []
    for item_user in redis_cache.hscan_iter(lang + ':users', count=1000):
        list_users.append(item_user)

    total_users = len(list_users)

    for item_user in list_users:
        try:
            writer_users.writerow([s if isinstance(s, str)
                                   else str(s) for s in item_user])
        except Exception as e:
            print("Error writing CSV file for registered users...")
            print(e)
    file_users.close()
    del list_users

    # Users with ID = 0 in dump file
    path_file_users_zero = os.path.join(tmp_dir,
                                        lang + '_users_zero.csv')
    file_users_zero = open(path_file_users_zero, 'w')
    writer_users_zero = csv.writer(file_users_zero, dialect='excel-tab',
                                   lineterminator='\n')

    list_users_zero = []
    for item_user_zero in redis_cache.hscan_iter(lang + ':userzero',
                                                 count=1000):
        list_users_zero.append(item_user_zero)

    total_users_zero = len(list_users_zero)

    for item_user_zero in list_users_zero:
        try:
            writer_users_zero.writerow([s if isinstance(s, str)
                                        else str(s) for s in item_user_zero])
        except Exception as e:
            print(e)

    file_users_zero.close()
    del list_users_zero

    print("Inserting anonymous revisions info in DB")
    con.send_query(insert_anons % path_file_anons)
    print("Inserting users info in DB")
    con.send_query(insert_users % path_file_users)
    print("Inserting missing users info in DB")
    print()
    con.send_query(insert_users_zero % path_file_users_zero)
    # TODO: Clean tmp files, uncomment the following lines
    # os.remove(path_file_anons)
    # os.remove(path_file_users)
    # Clean up Redis databases to free memory
#    redis_cache.delete(lang + ':revsanon', lang + ':users')
#    redis_cache.delete(lang + ':userzero')

    logging.info("COMPLETED: %s anonymous revisions processed %s." % (
                 total_anons,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))
    logging.info("COMPLETED: %s registered users processed %s." % (
                 total_users,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))

    logging.info("COMPLETED: %s users with missing ID processed %s." % (
                 total_users_zero,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))


def store_revs_db(rev_iter, con=None, log_file=None, size_cache=500):
    """
    Processor to insert revision info in DB
    """
    rev_insert_rows = 0
    total_revs = 0

    logging.basicConfig(filename=log_file, level=logging.DEBUG)
    logging.info("Starting parsing process...")

    # Retrieve item form intermediate worker
    for new_rev_insert, new_rev_hash in rev_iter:
        total_revs += 1
        # ### INSERT QUERIES BUILDING ###
        # First iteration
        # Always allow at least one row in extended inserts
        if rev_insert_rows == 0:
            # Case of people
            # First values are always 0: anonymous and -1:missing
            #user_insert = "".join(["INSERT INTO people ",
                                            #"VALUES(-1, 'NA'),",
                                            #"(0, 'Anonymous')"])
            #if new_user:
                #user_insert = "".join([user_insert, ",",
                                            #new_user_insert])
            # Case of revision
            rev_insert = "".join(["INSERT INTO revision ",
                                  "VALUES", new_rev_insert])

            # Case of revision_hash
            rev_hash = "".join(["INSERT INTO revision_hash VALUES",
                                new_rev_hash])
            # Update general rows counter
            rev_insert_rows += 1

        # Extended inserts not full yet
        # Append new row to rev_insert
        elif rev_insert_rows <= size_cache:
            # Case of people
            #if new_user:
                #if len(user_insert) > 0:
                    #user_insert = "".join([user_insert, ",",
                                                #new_user_insert])
                #else:
                    #user_insert = "".join(["INSERT INTO people ",
                                            #"VALUES", new_user_insert])

            # Case of revision
            rev_insert = "".join([rev_insert, ",",
                                  new_rev_insert])

            # Case of revision_hash
            rev_hash = "".join([rev_hash, ",",
                                new_rev_hash])
            # Update general rows counter
            rev_insert_rows += 1

        # Flush extended inserts and start over new queries
        else:
            # Case of people
            #if len(user_insert) > 0:
                #send_query(con, cursor, user_insert, 5,
                                #log_file)

                #if new_user:
                    #user_insert = "".join(["INSERT INTO people ",
                                                #"VALUES",
                                                #new_user_insert])
                #else:
                    #user_insert = ""
            #else:
                #if new_user:
                    #user_insert = "".join(["INSERT INTO people ",
                                                #"VALUES",
                                                #new_user_insert])

            # Case of revision
            con.send_query(rev_insert)
            rev_insert = "".join(["INSERT INTO revision ",
                                  "VALUES", new_rev_insert])
            # Case of revision_hash
            con.send_query(rev_hash)
            rev_hash = "".join(["INSERT INTO revision_hash ",
                                "VALUES", new_rev_hash])
            # Update general rows counter
            # print "total revisions: " + unicode(total_revs)
            rev_insert_rows = 1

        if total_revs % 10000 == 0:
            logging.info("%s revisions %s." % (
                         total_revs,
                         time.strftime("%Y-%m-%d %H:%M:%S %Z",
                                       time.localtime())))
#            print "%s revisions %s." % (
#                total_revs,
#                time.strftime("%Y-%m-%d %H:%M:%S %Z",
#                              time.localtime()))

    # Send last extended insert for revision
    con.send_query(rev_insert)

    # Send last extended insert for revision_hash
    con.send_query(rev_hash)

    logging.info("%s revisions %s." % (
                 total_revs,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))
    logging.info("END: %s revisions processed %s." % (
                 total_revs,
                 time.strftime("%Y-%m-%d %H:%M:%S %Z",
                               time.localtime())))

#    print "%s revisions %s." % (
#        total_revs,
#        time.strftime("%Y-%m-%d %H:%M:%S %Z",
#                      time.localtime()))
#    print "END: %s revisions processed %s." % (
#        total_revs, time.strftime("%Y-%m-%d %H:%M:%S %Z",
#                                  time.localtime()))


class RevisionText(DataItem):
    """
    Encapsulates rev_text elements for complex processing on their own
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor method for RevisionText objects. Must forward params to
        parent class DataItem (mandatory inheritance)
        """
        super(RevisionText, self).__init__(*args, **kwargs)
