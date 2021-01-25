#!/usr/bin/python3 -OO
# Copyright 2008-2017 The SABnzbd-Team <team@sabnzbd.org>
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.nzbparser - Parse and import NZB files
"""
import bz2
import gzip
import time
import logging
import hashlib
import xml.etree.ElementTree
import datetime
import re
import io

# import binascii

import sabnzbd
from sabnzbd import filesystem, nzbstuff
from sabnzbd.encoding import utob, correct_unknown_encoding
from sabnzbd.filesystem import is_archive, get_filename
from sabnzbd.misc import name_to_cat


def nzbfile_parser(raw_data, nzo):
    # Try regex parser
    if nzbfile_regex_parser(raw_data, nzo):
        return

    # Load data as file-object
    raw_data = raw_data.replace("http://www.newzbin.com/DTD/2003/nzb", "", 1)
    nzb_tree = xml.etree.ElementTree.fromstring(raw_data)

    # Hash for dupe-checking
    md5sum = hashlib.md5()

    # Average date
    avg_age_sum = 0

    # In case of failing timestamps and failing files
    time_now = time.time()
    skipped_files = 0
    valid_files = 0

    # Parse the header
    if nzb_tree.find("head"):
        for meta in nzb_tree.find("head").iter("meta"):
            meta_type = meta.attrib.get("type")
            if meta_type and meta.text:
                # Meta tags can occur multiple times
                if meta_type not in nzo.meta:
                    nzo.meta[meta_type] = []
                nzo.meta[meta_type].append(meta.text)
    logging.debug("NZB Meta-data = %s", nzo.meta)

    # Parse the files
    for file in nzb_tree.iter("file"):
        # Get subject and date
        file_name = ""
        if file.attrib.get("subject"):
            file_name = file.attrib.get("subject")

        # Don't fail if no date present
        try:
            file_date = datetime.datetime.fromtimestamp(int(file.attrib.get("date")))
            file_timestamp = int(file.attrib.get("date"))
        except:
            file_date = datetime.datetime.fromtimestamp(time_now)
            file_timestamp = time_now

        # Get group
        for group in file.iter("group"):
            if group.text not in nzo.groups:
                nzo.groups.append(group.text)

        # Get segments
        raw_article_db = {}
        file_bytes = 0
        if file.find("segments"):
            for segment in file.find("segments").iter("segment"):
                try:
                    article_id = segment.text
                    segment_size = int(segment.attrib.get("bytes"))
                    partnum = int(segment.attrib.get("number"))

                    # Update hash
                    md5sum.update(utob(article_id))

                    # Duplicate parts?
                    if partnum in raw_article_db:
                        if article_id != raw_article_db[partnum][0]:
                            logging.info(
                                "Duplicate part %s, but different ID-s (%s // %s)",
                                partnum,
                                raw_article_db[partnum][0],
                                article_id,
                            )
                            nzo.increase_bad_articles_counter("duplicate_articles")
                        else:
                            logging.info("Skipping duplicate article (%s)", article_id)
                    elif segment_size <= 0 or segment_size >= 2 ** 23:
                        # Perform sanity check (not negative, 0 or larger than 8MB) on article size
                        # We use this value later to allocate memory in cache and sabyenc
                        logging.info("Skipping article %s due to strange size (%s)", article_id, segment_size)
                        nzo.increase_bad_articles_counter("bad_articles")
                    else:
                        raw_article_db[partnum] = (article_id, segment_size)
                        file_bytes += segment_size
                except:
                    # In case of missing attributes
                    pass

        # Sort the articles by part number, compatible with Python 3.5
        raw_article_db_sorted = [raw_article_db[partnum] for partnum in sorted(raw_article_db)]

        # Create NZF
        nzf = sabnzbd.nzbstuff.NzbFile(file_date, file_name, raw_article_db_sorted, file_bytes, nzo)

        # Check if we already have this exact NZF (see custom eq-checks)
        if nzf in nzo.files:
            logging.info("File %s occured twice in NZB, skipping", nzf.filename)
            continue

        # Add valid NZF's
        if file_name and nzf.valid and nzf.nzf_id:
            logging.info("File %s added to queue", nzf.filename)
            nzo.files.append(nzf)
            nzo.files_table[nzf.nzf_id] = nzf
            nzo.bytes += nzf.bytes
            valid_files += 1
            avg_age_sum += file_timestamp
        else:
            logging.info("Error importing %s, skipping", file_name)
            if nzf.nzf_id:
                sabnzbd.remove_data(nzf.nzf_id, nzo.admin_path)
            skipped_files += 1

    # Final bookkeeping
    nr_files = max(1, valid_files)
    nzo.avg_stamp = avg_age_sum / nr_files
    nzo.avg_date = datetime.datetime.fromtimestamp(avg_age_sum / nr_files)
    nzo.md5sum = md5sum.hexdigest()

    if skipped_files:
        logging.warning(T("Failed to import %s files from %s"), skipped_files, nzo.filename)


def nzbfile_regex_parser(raw_data, nzo):
    # Hash for dupe-checking
    md5sum = hashlib.md5()

    # Average date
    avg_age_sum = 0

    # In case of failing timestamps and failing files
    time_now = time.time()
    valid_files = 0

    success = 1

    group_re = re.compile("^\s*(?:<groups>\s*|)<group>(.*?)</group>(?:\s*</groups>|)\s*$")
    file_re = re.compile("^\s*<file(.*?)>\s*$")
    fileend_re = re.compile("^\s*</file>\s*$")
    segment_re = re.compile("^\s*<segment( .*?)>(.*?)</segment>\s*$")
    ignorable_re = re.compile(
        "^\s*</(segments?|groups|head)>\s*$|^\s*<(groups|segments|head|nzb[^>]*)>\s*$|^\s*<!--.*-->\s*$"
    )
    whitespace_re = re.compile("^\s*$")

    subject_re = re.compile(' subject="(.*?)"')
    date_re = re.compile(' date="(.*?)"')

    bytes_re = re.compile(' bytes="(.*?)"')
    number_re = re.compile(' number="(.*?)"')

    encoding_re = re.compile('<\?xml [^>]*encoding="(.*?)"')
    meta_re = re.compile('^\s*<meta type="(.*?)">(.*?)</meta>\s*$')
    nzbtag_re = re.compile("^\s*<nzb xmlns.*?>\s*$")
    # Can be comments near the end tag
    endnzb_re = re.compile(
        "^\s*(?:<!--[^<]*-->|)\s*(?:<!--[^<]*-->|)\s*</nzb>\s*(?:<!--[^<]*-->|)\s*(?:<!--[^<]*-->|)\s*$"
    )

    try:
        reader = io.StringIO(raw_data)
        open_file_tag = 0
        linecount = 0
        encoding = ""
        res = 0
        header = ""

        # Read header data until <nzb tag
        while not res:
            line = reader.readline()
            if line == "\n":
                continue
            linecount += 1
            if linecount > 10:
                raise Exception("Could not find <nzb tag in header: %s" % header)
            header += line.replace("\n", " ")
            res = nzbtag_re.search(line)

        # Get encoding (sanity check)
        res = encoding_re.search(header)
        if res:
            encoding = res.group(1)
        else:
            raise Exception("Could not find encoding in header: %s" % header)

        # Read the rest of the file
        for line in reader:
            if line == "\n":
                continue
            linecount += 1

            # <segment bytes="100" number="1">articleid</segment>
            res = segment_re.search(line)
            if res:
                if not open_file_tag:
                    raise Exception("Found segment without file tag at line %s: %s" % (linecount, line))
                article_id = res.group(2)
                segment_size = int(bytes_re.search(res.group(1)).group(1))
                partnum = int(number_re.search(res.group(1)).group(1))

                # Update hash
                md5sum.update(utob(article_id))

                # Duplicate parts?
                if partnum in raw_article_db:
                    if article_id != raw_article_db[partnum][0]:
                        raise Exception(
                            "Duplicate part %s, but different ID-s (%s // %s)"
                            % (partnum, raw_article_db[partnum][0], article_id)
                        )
                    else:
                        raise Exception("Duplicate article (%s)" % article_id)
                elif segment_size <= 0 or segment_size >= 2 ** 23:
                    # Perform sanity check (not negative, 0 or larger than 8MB) on article size
                    # We use this value later to allocate memory in cache and sabyenc
                    raise Exception("Article %s has strange size (%s)" % (article_id, segment_size))
                else:
                    raw_article_db[partnum] = (article_id, segment_size)
                    file_bytes += segment_size
                    continue

            # </file>
            res = fileend_re.search(line)
            if res:
                if open_file_tag:
                    open_file_tag = 0
                else:
                    raise Exception("Found closing file tag without start at line %s: %s" % (linecount, line))

                if not file_name:
                    raise Exception("Found closing file tag with no file_name at line %s: %s" % (linecount, line))

                # Sort the articles by part number, compatible with Python 3.5
                raw_article_db_sorted = [raw_article_db[partnum] for partnum in sorted(raw_article_db)]

                # Create NZF
                nzf = sabnzbd.nzbstuff.NzbFile(file_date, file_name, raw_article_db_sorted, file_bytes, nzo)

                # Check if we already have this exact NZF (see custom eq-checks)
                if nzf in nzo.files:
                    logging.info("File %s occured twice in NZB, skipping", nzf.filename)
                    continue

                # Add valid NZF's
                if nzf.valid and nzf.nzf_id:
                    logging.info("File %s added to queue", nzf.filename)
                    nzo.files.append(nzf)
                    nzo.files_table[nzf.nzf_id] = nzf
                    nzo.bytes += nzf.bytes
                    valid_files += 1
                    avg_age_sum += file_timestamp
                    continue
                else:
                    raise Exception(
                        "Found closing file tag with invalid nzf (valid %s, nzf_id %s) at line %s: %s"
                        % (nzf.valid, nzf.nzf_id, linecount, line)
                    )

            # Junk
            res = ignorable_re.search(line)
            if res:
                continue

            # <file>
            res = file_re.search(line)
            if res:
                if open_file_tag:
                    raise Exception("Found open file tag when already in a fil at line %s: %s" % (linecount, line))
                else:
                    open_file_tag = 1

                raw_article_db = {}
                file_bytes = 0

                file_name = subject_re.search(res.group(1)).group(1)
                tmpdate = date_re.search(res.group(1))
                # Don't fail if no date present
                try:
                    file_date = datetime.datetime.fromtimestamp(int(tmpdate.group(1)))
                    file_timestamp = int(tmpdate.group(1))
                except:
                    file_date = datetime.datetime.fromtimestamp(time_now)
                    file_timestamp = time_now
                continue

            # <group>a.b.a</group>
            res = group_re.search(line)
            if res:
                # logging.debug("Got group")
                if res.group(1) not in nzo.groups:
                    nzo.groups.append(res.group(1))
                continue

            # <meta type="password">password123</meta>
            res = meta_re.search(line)
            if res:
                # logging.debug("Got meta")
                meta_type = res.group(1)
                meta_text = res.group(2)
                if meta_type and meta_text:
                    # Meta tags can occur multiple times
                    if meta_type not in nzo.meta:
                        nzo.meta[meta_type] = []
                    nzo.meta[meta_type].append(meta_text)
                continue

            res = whitespace_re.search(line)
            if res:
                continue

            # </nzb>
            res = endnzb_re.search(line)
            if res:
                if open_file_tag:
                    raise Exception("Found closing <nzb tag while in file at line %s: %s" % (linecount, line))
                break

            # logging.debug("Line %s: %s", linecount, binascii.hexlify(line.encode()))
            # raise Exception("Unrecognized line #%s: %s (%s)" % (linecount, line, binascii.hexlify(line.encode())))
            raise Exception("Unrecognized line #%s: %s" % (linecount, line))
    except Exception as e:
        logging.warning("Regex parsing of %s failed: %s", nzo.filename, e)
        success = 0

    if success:
        # Final bookkeeping
        logging.debug("NZB Meta-data = %s", nzo.meta)
        nr_files = max(1, valid_files)
        nzo.avg_stamp = avg_age_sum / nr_files
        nzo.avg_date = datetime.datetime.fromtimestamp(avg_age_sum / nr_files)
        nzo.md5sum = md5sum.hexdigest()
        return True
    else:
        # Remove all data added to the nzo
        for nzf in nzo.files:
            nzf.remove_admin()
        nzo.first_articles = []
        nzo.first_articles_count = 0
        nzo.bytes_par2 = 0
        nzo.files = []
        nzo.files_table = {}
        nzo.bytes = 0
        return False


def process_nzb_archive_file(
    filename,
    path,
    pp=None,
    script=None,
    cat=None,
    catdir=None,
    keep=False,
    priority=None,
    nzbname=None,
    reuse=None,
    nzo_info=None,
    dup_check=True,
    url=None,
    password=None,
    nzo_id=None,
):
    """Analyse ZIP file and create job(s).
    Accepts ZIP files with ONLY nzb/nfo/folder files in it.
    returns (status, nzo_ids)
        status: -1==Error, 0==OK, 1==Ignore
    """
    nzo_ids = []
    if catdir is None:
        catdir = cat

    filename, cat = name_to_cat(filename, catdir)
    # Returns -1==Error/Retry, 0==OK, 1==Ignore
    status, zf, extension = is_archive(path)

    if status != 0:
        return status, []

    status = 1
    names = zf.namelist()
    nzbcount = 0
    for name in names:
        name = name.lower()
        if name.endswith(".nzb"):
            status = 0
            nzbcount += 1

    if status == 0:
        if nzbcount != 1:
            nzbname = None
        for name in names:
            if name.lower().endswith(".nzb"):
                try:
                    data = correct_unknown_encoding(zf.read(name))
                except OSError:
                    logging.error(T("Cannot read %s"), name, exc_info=True)
                    zf.close()
                    return -1, []
                name = filesystem.setname_from_path(name)
                if data:
                    nzo = None
                    try:
                        nzo = nzbstuff.NzbObject(
                            name,
                            pp=pp,
                            script=script,
                            nzb=data,
                            cat=cat,
                            url=url,
                            priority=priority,
                            nzbname=nzbname,
                            nzo_info=nzo_info,
                            reuse=reuse,
                            dup_check=dup_check,
                        )
                        if not nzo.password:
                            nzo.password = password
                    except (TypeError, ValueError):
                        # Duplicate or empty, ignore
                        pass
                    except:
                        # Something else is wrong, show error
                        logging.error(T("Error while adding %s, removing"), name, exc_info=True)

                    if nzo:
                        if nzo_id:
                            # Re-use existing nzo_id, when a "future" job gets it payload
                            sabnzbd.NzbQueue.remove(nzo_id, delete_all_data=False)
                            nzo.nzo_id = nzo_id
                            nzo_id = None
                        nzo_ids.append(sabnzbd.NzbQueue.add(nzo))
                        nzo.update_rating()
        zf.close()
        try:
            if not keep:
                filesystem.remove_file(path)
        except OSError:
            logging.error(T("Error removing %s"), filesystem.clip_path(path))
            logging.info("Traceback: ", exc_info=True)
    else:
        zf.close()
        status = 1

    return status, nzo_ids


def process_single_nzb(
    filename,
    path,
    pp=None,
    script=None,
    cat=None,
    catdir=None,
    keep=False,
    priority=None,
    nzbname=None,
    reuse=None,
    nzo_info=None,
    dup_check=True,
    url=None,
    password=None,
    nzo_id=None,
):
    """Analyze file and create a job from it
    Supports NZB, NZB.BZ2, NZB.GZ and GZ.NZB-in-disguise
    returns (status, nzo_ids)
        status: -2==Error/retry, -1==Error, 0==OK
    """
    nzo_ids = []
    if catdir is None:
        catdir = cat

    try:
        with open(path, "rb") as nzb_file:
            check_bytes = nzb_file.read(2)

        if check_bytes == b"\x1f\x8b":
            # gzip file or gzip in disguise
            filename = filename.replace(".nzb.gz", ".nzb")
            nzb_reader_handler = gzip.GzipFile
        elif check_bytes == b"BZ":
            # bz2 file or bz2 in disguise
            filename = filename.replace(".nzb.bz2", ".nzb")
            nzb_reader_handler = bz2.BZ2File
        else:
            nzb_reader_handler = open

        # Let's get some data and hope we can decode it
        with nzb_reader_handler(path, "rb") as nzb_file:
            data = correct_unknown_encoding(nzb_file.read())

    except OSError:
        logging.warning(T("Cannot read %s"), filesystem.clip_path(path))
        logging.info("Traceback: ", exc_info=True)
        return -2, nzo_ids

    if filename:
        filename, cat = name_to_cat(filename, catdir)
        # The name is used as the name of the folder, so sanitize it using folder specific santization
        if not nzbname:
            # Prevent embedded password from being damaged by sanitize and trimming
            nzbname = get_filename(filename)

    try:
        nzo = nzbstuff.NzbObject(
            filename,
            pp=pp,
            script=script,
            nzb=data,
            cat=cat,
            url=url,
            priority=priority,
            nzbname=nzbname,
            nzo_info=nzo_info,
            reuse=reuse,
            dup_check=dup_check,
        )
        if not nzo.password:
            nzo.password = password
    except TypeError:
        # Duplicate, ignore
        if nzo_id:
            sabnzbd.NzbQueue.remove(nzo_id)
        nzo = None
    except ValueError:
        # Empty
        return 1, nzo_ids
    except:
        if data.find("<nzb") >= 0 > data.find("</nzb"):
            # Looks like an incomplete file, retry
            return -2, nzo_ids
        else:
            # Something else is wrong, show error
            logging.error(T("Error while adding %s, removing"), filename, exc_info=True)
            return -1, nzo_ids

    if nzo:
        if nzo_id:
            # Re-use existing nzo_id, when a "future" job gets it payload
            sabnzbd.NzbQueue.remove(nzo_id, delete_all_data=False)
            nzo.nzo_id = nzo_id
        nzo_ids.append(sabnzbd.NzbQueue.add(nzo, quiet=reuse))
        nzo.update_rating()

    try:
        if not keep:
            filesystem.remove_file(path)
    except OSError:
        # Job was still added to the queue, so throw error but don't report failed add
        logging.error(T("Error removing %s"), filesystem.clip_path(path))
        logging.info("Traceback: ", exc_info=True)

    return 0, nzo_ids
