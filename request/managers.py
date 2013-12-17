import datetime
import time
from django.utils.timezone import utc
import calendar

from django.db import models
from django.contrib.sessions.models import Session
from django.core.cache import cache

from request import settings
from request.utils import request_cache_key

try:  # For python <= 2.3
    set()
except NameError:
    from sets import Set as set

QUERYSET_PROXY_METHODS = ('year', 'month', 'week', 'day', 'today', 'this_week', 'this_month', 'this_year', 'unique_visits', 'attr_list', 'search')


class RequestQuerySet(models.query.QuerySet):
    def year(self, year):
        return self.filter(time__year=year)

    def month(self, year=None, month=None, month_format='%b', date=None):
        if not date:
            try:
                if year and month:
                    date = datetime.date(*time.strptime(year + month, '%Y' + month_format)[:3])
                else:
                    raise TypeError('Request.objects.month() takes exactly 2 arguments')
            except ValueError:
                return

        weekday, number_days = calendar.monthrange(date.year, date.month)

        # Calculate first and last day of month, for use in a date-range lookup.
        first_day = date.replace(day=1)
        last_day = date.replace(day=number_days) + datetime.timedelta(1)

        lookup_kwargs = {
            'time__gte': first_day,
            'time__lt': last_day,
        }

        return self.filter(**lookup_kwargs)

    def week(self, year, week):
        try:
            date = datetime.date(*time.strptime(year + '-0-' + week, '%Y-%w-%U')[:3]).replace(tzinfo=utc)
        except ValueError:
            return

        # Calculate first and last day of week, for use in a date-range lookup.
        first_day = date
        last_day = date + datetime.timedelta(days=7)
        lookup_kwargs = {
            'time__gte': first_day,
            'time__lt': last_day,
        }

        return self.filter(**lookup_kwargs)

    def day(self, year=None, month=None, day=None, month_format='%b', day_format='%d', date=None):
        if not date:
            try:
                if year and month and day:
                    date = datetime.datetime.date(*time.strptime(year + month + day, '%Y' + month_format + day_format)[:3])
                else:
                    raise TypeError('Request.objects.day() takes exactly 3 arguments')
            except ValueError:
                return

        dt_start = datetime.datetime.combine(date, datetime.time.min).replace(tzinfo=utc)
        dt_end = datetime.datetime.combine(date, datetime.time.max).replace(tzinfo=utc)
        return self.filter(time__range=(dt_start, dt_end))

    def today(self):
        return self.day(date=datetime.datetime.utcnow().replace(tzinfo=utc))

    def this_year(self):
        return self.year(datetime.datetime.utcnow().replace(tzinfo=utc).year)

    def this_month(self):
        return self.month(date=datetime.datetime.utcnow().replace(tzinfo=utc))

    def this_week(self):
        today = datetime.datetime.utcnow().replace(tzinfo=utc)
        return self.week(str(today.year), str(today.isocalendar()[1] - 1))

    def unique_visits(self):
        return self.exclude(referer__startswith=settings.REQUEST_BASE_URL)

    def attr_list(self, name):
        return [getattr(item, name, None) for item in self if hasattr(item, name)]

    def search(self):
        return self.filter(referer__contains='google') | self.filter(referer__contains='yahoo') | self.filter(referer__contains='bing')


class RequestManager(models.Manager):
    def __getattr__(self, attr, *args, **kwargs):
        if attr in QUERYSET_PROXY_METHODS:
            return getattr(self.get_query_set(), attr, None)
        super(RequestManager, self).__getattr__(*args, **kwargs)

    def get_query_set(self):
        return RequestQuerySet(self.model)

    def active_users(self, **options):
        """
        Returns a list of active users.

        Any arguments passed to this method will be
        given to timedelta for time filtering.

        Example:
        >>> Request.object.active_users(minutes=15)
        [<User: kylef>, <User: krisje8>]
        """

        qs = self.filter(user__isnull=False)

        if options:
            time = datetime.datetime.utcnow().replace(tzinfo=utc) - datetime.timedelta(**options)
            qs = qs.filter(time__gte=time)

        requests = qs.select_related('user').only('user')

        return set([request.user for request in requests])

    def create_from_http_request(self, request, response=None, commit=True):
        r = self.model()
        r.from_http_request(request, response)

        # Save the request to the cache
        if commit:
            if settings.REQUEST_USE_CACHE == True:
                cache_key = request_cache_key(r)
                cache.set(cache_key, r, timeout=0)

            elif settings.REQUEST_BUFFER_SIZE == 0:
                r.save()
            else:
                settings.REQUEST_BUFFER.append(r)
                if len(settings.REQUEST_BUFFER) > settings.REQUEST_BUFFER_SIZE:
                    try:
                        self.bulk_create(settings.REQUEST_BUFFER)
                    except:
                        pass
                    settings.REQUEST_BUFFER = []
    
    def persist_cached(self, cache_pattern=None):
        """
        Fetches all requests stored in the cache and push them to the database.
        Returns the persisted requests.
        """
        created = []

        if settings.REQUEST_USE_CACHE:
            # Set default cache pattern if not given
            if not cache_pattern:
                cache_pattern = '%s*' % settings.REQUEST_CACHE_PREFIX

            # Get all requests from cache
            requests_keys = cache.keys(cache_pattern)
            requests_dict = cache.get_many(requests_keys)
            requests = requests_dict.values()
            
            # Persist all requests to database
            created = self.bulk_create(requests)

            # Clear requests cache
            cache.delete_pattern(cache_pattern)

        return created

    def last_requests_with_open_sessions_from_users(self, user_ids):
        """
        Returns a SQL cursor with the list of newest request with open session
        grouped by session for a given list of user ids
        """
        # Parse list of ids to int
        user_ids = map(lambda x: int(x), user_ids)

        if len(user_ids) == 1:
            # copy twice otherwise the sql query doesn't work
            user_ids.append(user_ids[0])

        now = datetime.datetime.utcnow().replace(tzinfo=utc)

        # Store cached requests in the database before querying
        self.persist_cached()
        
        # It cannot be done via ORM with one query since there is no relationship between the two tables
        query = """SELECT max(r.id) as id \
                   FROM request_request r, django_session s \
                   WHERE r.user_id IN %s and \
                         s.expire_date >= %s and \
                         r.session_key = s.session_key \
                   GROUP BY s.session_key \
                   ORDER BY r.time DESC"""

        # Get active session keys from database-stored requests
        requests = self.raw(query, [user_ids, now])

        return requests

    def get_open_session_keys_from_users(self, user_ids):
        """
        Returns a SQL cursor with the list of keys of open session
        for a given list of user ids
        """
        requests = self.last_requests_with_open_sessions_from_users(user_ids)
        session_keys = [r.session_key for r in requests]

        return session_keys
