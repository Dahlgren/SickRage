# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement

import time
import traceback
import threading

import sickbeard
from sickbeard import db, logger, common, exceptions, helpers
from sickbeard import generic_queue, scheduler
from sickbeard import search, failed_history, history
from sickbeard import ui
from sickbeard.exceptions import ex
from sickbeard.search import pickBestResult

search_queue_lock = threading.Lock()

BACKLOG_SEARCH = 10
DAILY_SEARCH = 20
FAILED_SEARCH = 30
MANUAL_SEARCH = 30

class SearchQueue(generic_queue.GenericQueue):
    def __init__(self):
        generic_queue.GenericQueue.__init__(self)
        self.queue_name = "SEARCHQUEUE"

    def is_in_queue(self, show, segment):
        queue =  [x for x in self.queue.queue] + [self.currentItem]
        for cur_item in queue:
            if cur_item:
                if cur_item.show == show and cur_item.segment == segment:
                    return True
        return False

    def pause_backlog(self):
        self.min_priority = generic_queue.QueuePriorities.HIGH

    def unpause_backlog(self):
        self.min_priority = 0

    def is_backlog_paused(self):
        # backlog priorities are NORMAL, this should be done properly somewhere
        return self.min_priority >= generic_queue.QueuePriorities.NORMAL

    def is_backlog_in_progress(self):
        queue = [x for x in self.queue.queue] + [self.currentItem]
        for cur_item in queue:
            if isinstance(cur_item, BacklogQueueItem):
                return True
        return False

    def add_item(self, item):

        if isinstance(item, DailySearchQueueItem) and not self.is_in_queue(item.show, item.segment):
            generic_queue.GenericQueue.add_item(self, item)
        elif isinstance(item, BacklogQueueItem) and not self.is_in_queue(item.show, item.segment):
            generic_queue.GenericQueue.add_item(self, item)
        elif isinstance(item, ManualSearchQueueItem) and not self.is_in_queue(item.show, item.segment):
            generic_queue.GenericQueue.add_item(self, item)
        elif isinstance(item, FailedQueueItem) and not self.is_in_queue(item.show, item.segment):
            generic_queue.GenericQueue.add_item(self, item)
        else:
            logger.log(u"Not adding item, it's already in the queue", logger.DEBUG)

class DailySearchQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Daily Search', DAILY_SEARCH)
        self.priority = generic_queue.QueuePriorities.HIGH
        self.thread_name = 'DAILYSEARCH-' + str(show.indexerid)
        self.show = show
        self.segment = segment

    def execute(self):
        generic_queue.QueueItem.execute(self)

        logger.log("Beginning daily search for [" + self.show.name + "]")
        foundResults = search.searchForNeededEpisodes(self.segment)

        # reset thread back to original name
        threading.currentThread().name = self.thread_name

        if not len(foundResults):
            logger.log(u"No needed episodes found during daily search for [" + self.show.name + "]")
        else:
            for curEp in foundResults:
                for result in curEp:
                    # just use the first result for now
                    logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                    search.snatchEpisode(result)

                    # give the CPU a break
                    time.sleep(2)

        generic_queue.QueueItem.finish(self)

class ManualSearchQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Manual Search', MANUAL_SEARCH)
        self.priority = generic_queue.QueuePriorities.HIGH
        self.thread_name = 'MANUAL-' + str(show.indexerid)
        self.success = None
        self.show = show
        self.segment = segment

    def execute(self):
        generic_queue.QueueItem.execute(self)

        try:
            logger.log("Beginning manual search for [" + self.segment.prettyName() + "]")
            searchResult = search.searchProviders(self.show, self.segment.season, [self.segment], True)

            # reset thread back to original name
            threading.currentThread().name = self.thread_name

            if searchResult:
                # just use the first result for now
                logger.log(u"Downloading " + searchResult[0].name + " from " + searchResult[0].provider.name)
                self.success = search.snatchEpisode(searchResult[0])

                # give the CPU a break
                time.sleep(2)

            else:
                ui.notifications.message('No downloads were found',
                                         "Couldn't find a download for <i>%s</i>" % self.segment.prettyName())

                logger.log(u"Unable to find a download for " + self.segment.prettyName())

        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)

    def finish(self):
        # don't let this linger if something goes wrong
        if self.success == None:
            self.success = False
        generic_queue.QueueItem.finish(self)

class BacklogQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Backlog', BACKLOG_SEARCH)
        self.priority = generic_queue.QueuePriorities.LOW
        self.thread_name = 'BACKLOG-' + str(show.indexerid)
        self.success = None
        self.show = show
        self.segment = segment

    def execute(self):
        generic_queue.QueueItem.execute(self)

        for season in self.segment:
            sickbeard.searchBacklog.BacklogSearcher.currentSearchInfo = {'title': self.show.name + " Season " + str(season)}

            wantedEps = self.segment[season]

            try:
                logger.log("Beginning backlog search for [" + self.show.name + "]")
                searchResult = search.searchProviders(self.show, season, wantedEps, False)

                # reset thread back to original name
                threading.currentThread().name = self.thread_name

                if searchResult:
                    for result in searchResult:
                        # just use the first result for now
                        logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                        search.snatchEpisode(result)

                        # give the CPU a break
                        time.sleep(2)

                else:
                    logger.log(u"No needed episodes found during backlog search for [" + self.show.name + "]")

            except Exception:
                logger.log(traceback.format_exc(), logger.DEBUG)

        self.finish()

class FailedQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Retry', FAILED_SEARCH)
        self.priority = generic_queue.QueuePriorities.HIGH
        self.thread_name = 'RETRY-' + str(show.indexerid)
        self.show = show
        self.segment = segment
        self.success = None

    def execute(self):
        generic_queue.QueueItem.execute(self)

        failed_episodes = []
        for season in self.segment:
            epObj = self.segment[season]

            (release, provider) = failed_history.findRelease(epObj)
            if release:
                logger.log(u"Marking release as bad: " + release)
                failed_history.markFailed(epObj)
                failed_history.logFailed(release)
                history.logFailed(epObj, release, provider)
                failed_history.revertEpisode(epObj)
                failed_episodes.append(epObj)

                logger.log(
                    "Beginning failed download search for [" + epObj.prettyName() + "]")

        if len(failed_episodes):
            try:
                searchResult = search.searchProviders(self.show, failed_episodes[0].season, failed_episodes, True)

                # reset thread back to original name
                threading.currentThread().name = self.thread_name

                if searchResult:
                    for result in searchResult:
                        # just use the first result for now
                        logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                        search.snatchEpisode(result)

                        # give the CPU a break
                        time.sleep(2)

                else:
                    logger.log(u"No episodes found to retry for failed downloads return from providers!")
            except Exception, e:
                logger.log(traceback.format_exc(), logger.DEBUG)

        self.finish()