#!/usr/bin/python -OO
# Copyright 2008-2012 The SABnzbd-Team <team@sabnzbd.org>
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
#
"""
sabnzbd.growler - Send notifications to Growl
"""
#------------------------------------------------------------------------------
from __future__ import with_statement
import os.path
import logging
import socket
import time
import subprocess
from threading import Thread

import sabnzbd
import sabnzbd.cfg
from sabnzbd.encoding import unicoder, latin1
from sabnzbd.constants import NOTIFY_KEYS
from gntp import GNTPRegister
from gntp.notifier import GrowlNotifier
try:
    import Growl
    # Detect classic Growl (older than 1.3)
    _HAVE_CLASSIC_GROWL = os.path.isfile('/Library/PreferencePanes/Growl.prefPane/Contents/MacOS/Growl')
except ImportError:
    _HAVE_CLASSIC_GROWL = False
try:
    import warnings
    # Make any warnings exceptions, so that pynotify is ignored
    # PyNotify will not work with Python 2.5 (due to next three lines)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        import pynotify
    _HAVE_NTFOSD = True
except:
    _HAVE_NTFOSD = False

#------------------------------------------------------------------------------
# Define translatable message table
TT = lambda x:x
NOTIFICATION = {
    'startup'  : TT('Startup/Shutdown'),        #: Message class for Growl server
    'download' : TT('Added NZB'),               #: Message class for Growl server
    'pp'       : TT('Post-processing started'), #: Message class for Growl server
    'complete' : TT('Job finished'),            #: Message class for Growl server
    'other'    : TT('Other Messages')           #: Message class for Growl server
}

#------------------------------------------------------------------------------
# Setup platform dependent Growl support
#
_GROWL = None       # Instance of the Notifier after registration
_GROWL_REG = False  # Succesful registration


#------------------------------------------------------------------------------
def get_icon():
    icon = os.path.join(os.path.join(sabnzbd.DIR_PROG, 'icons'), 'sabnzbd.ico')
    if not os.path.isfile(icon):
        icon = os.path.join(sabnzbd.DIR_PROG, 'sabnzbd.ico')
    if os.path.isfile(icon):
        if sabnzbd.WIN32 or sabnzbd.DARWIN:
            fp = open(icon, 'rb')
            icon = fp.read()
            fp.close
        else:
            # Due to a bug in GNTP, need this work-around for Linux/Unix
            icon = 'http://sabnzbdplus.sourceforge.net/version/sabnzbd.ico'
    else:
        icon = None
    return icon


#------------------------------------------------------------------------------
def change_value():
    """ Signal that we should register with a new Growl server
    """
    global _GROWL_REG
    _GROWL_REG = False


#------------------------------------------------------------------------------
def have_ntfosd():
    """ Return if any PyNotify support is present
    """
    return bool(_HAVE_NTFOSD)


#------------------------------------------------------------------------------
def send_notification(title , msg, gtype, wait=False):
    """ Send Notification message
        Return '' when OK, otherwise an error string
    """
    res = []
    if gtype in sabnzbd.cfg.notify_classes() or wait:
        if sabnzbd.DARWIN_ML and sabnzbd.cfg.ncenter_enable():
            res.append(send_notification_center(title, msg, gtype))
        if sabnzbd.cfg.growl_enable():
            if _HAVE_CLASSIC_GROWL and not sabnzbd.cfg.growl_server():
                return send_local_growl(title, msg, gtype)
            else:
                if wait:
                    res.append(send_growl(title, msg, gtype))
                else:
                    res.append('ok')
                    Thread(target=send_growl, args=(title, msg, gtype)).start()
                    time.sleep(0.5)
        if have_ntfosd():
            res.append(send_notify_osd(title, msg))

    return ' / '.join([r for r in res if r])

#------------------------------------------------------------------------------
def reset_growl():
    """ Reset Growl (after changing language)
    """
    global _GROWL, _GROWL_REG
    _GROWL = None
    _GROWL_REG = False


#------------------------------------------------------------------------------
def register_growl():
    """ Register this app with Growl
    """
    error = None
    host, port = sabnzbd.misc.split_host(sabnzbd.cfg.growl_server())

    sys_name = hostname(host)

    # Clean up persistent data in GNTP to make re-registration work
    GNTPRegister.notifications = []
    GNTPRegister.headers = {}

    growler = GrowlNotifier(
        applicationName = 'SABnzbd%s' % sys_name,
        applicationIcon = get_icon(),
        notifications = [Tx(NOTIFICATION[key]) for key in NOTIFY_KEYS],
        hostname = host or 'localhost',
        port = port or 23053,
        password = sabnzbd.cfg.growl_password() or None
    )

    try:
        ret = growler.register()
        if ret is None or isinstance(ret, bool):
            logging.info('Registered with Growl')
            ret = growler
        else:
            error = 'Cannot register with Growl %s' % str(ret)
            logging.debug(error)
            del growler
            ret = None
    except socket.error, err:
        error = 'Cannot register with Growl %s' % str(err)
        logging.debug(error)
        del growler
        ret = None
    except:
        error = 'Unknown Growl registration error'
        logging.debug(error)
        logging.info("Traceback: ", exc_info = True)
        del growler
        ret = None

    return ret, error


#------------------------------------------------------------------------------
def send_growl(title , msg, gtype):
    """ Send Growl message
    """
    global _GROWL, _GROWL_REG

    for n in (0, 1):
        if not _GROWL_REG: _GROWL = None
        if not _GROWL:
            _GROWL, error = register_growl()
        if _GROWL:
            assert isinstance(_GROWL, GrowlNotifier)
            _GROWL_REG = True
            if not isinstance(msg, str) and not isinstance(msg, unicode):
                msg = str(msg)
            logging.debug('Send to Growl: %s %s %s', gtype, latin1(title), latin1(msg))
            try:
                ret = _GROWL.notify(
                    noteType = Tx(NOTIFICATION.get(gtype, 'other')),
                    title = title,
                    description = unicoder(msg),
                )
                if ret is None or isinstance(ret, bool):
                    return None
                elif ret[0] == '401':
                    _GROWL = False
                else:
                    logging.debug('Growl error %s', ret)
                    return 'Growl error %s', ret
            except socket.error, err:
                error = 'Growl error %s' % err
                logging.debug(error)
                return error
            except:
                error = 'Growl error (unknown)'
                logging.debug(error)
                return error
        else:
            return error
    return None

#------------------------------------------------------------------------------
# Local OSX Growl support
#
if _HAVE_CLASSIC_GROWL:
    _local_growl = None
    if os.path.isfile('sabnzbdplus.icns'):
        _OSX_ICON = Growl.Image.imageFromPath('sabnzbdplus.icns')
    elif os.path.isfile('osx/resources/sabnzbdplus.icns'):
        _OSX_ICON = Growl.Image.imageFromPath('osx/resources/sabnzbdplus.icns')
    else:
        _OSX_ICON = Growl.Image.imageWithIconForApplication('Terminal')

    def send_local_growl(title , msg, gtype):
        """ Send to local Growl server, OSX-only """
        global _local_growl
        if not _local_growl:
            notes = [Tx(NOTIFICATION[key]) for key in NOTIFY_KEYS]
            _local_growl = Growl.GrowlNotifier(
                applicationName = 'SABnzbd',
                applicationIcon = _OSX_ICON,
                notifications = notes,
                defaultNotifications = notes
                )
            _local_growl.register()
        _local_growl.notify(Tx(NOTIFICATION.get(gtype, 'other')), title, msg)
        return None


#------------------------------------------------------------------------------
# Ubuntu NotifyOSD Support
#
if _HAVE_NTFOSD:
    _NTFOSD = False
    def send_notify_osd(title, message):
        """ Send a message to NotifyOSD
        """
        global _NTFOSD
        error = 'NotifyOSD not working'
        if sabnzbd.cfg.ntfosd_enable():
            icon = os.path.join(sabnzbd.DIR_PROG, 'sabnzbd.ico')
            _NTFOSD = _NTFOSD or pynotify.init('icon-summary-body')
            if _NTFOSD:
                logging.info('Send to NotifyOSD: %s / %s', latin1(title), latin1(message))
                try:
                    note = pynotify.Notification(title, message, icon)
                    note.show()
                except:
                    # Apparently not implemented on this system
                    logging.info(error)
                    return error
                return None
            else:
                return error
        else:
            return 'Not enabled'


def ncenter_path():
    """ Return path of Notification Center tool, if it exists """
    tool = os.path.normpath(os.path.join(sabnzbd.DIR_PROG, '../Resources/SABnzbd.app/Contents/MacOS/SABnzbd'))
    if os.path.exists(tool):
        return tool
    else:
        return None

def send_notification_center(title, msg, gtype):
    """ Send message to Mountain Lion's Notification Center """
    tool = ncenter_path()    
    if tool:
        try:
            command = [tool, '-title', title, '-message', msg, '-group', Tx(NOTIFICATION.get(gtype, 'other'))]
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            output = proc.stdout.read()
            proc.wait()
            if 'Notification delivered' in output:
                output = ''
        except:
            logging.info('Cannot run notifier "%s"', tool)
            logging.debug("Traceback: ", exc_info = True)
            output = 'Notifier tool crashed'
    else:
        output = 'Notifier app not found'
    return output.strip('*\n ')


#------------------------------------------------------------------------------
def hostname(host=True):
    """ Return host's pretty name """
    if sabnzbd.WIN32:
        sys_name = os.environ.get('computername', 'unknown')
    else:
        try:
            sys_name = os.uname()[1]
        except:
            sys_name = 'unknown'
    if host:
        return '@%s' % sys_name.lower()
    else:
        return ''
