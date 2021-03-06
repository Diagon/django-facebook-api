# -*- coding: utf-8 -*-
'''
Copyright 2011-2015 ramusus
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''
import logging

from dateutil.parser import parse as datetime_parse
from django.contrib.contenttypes import generic
from django.contrib.contenttypes.models import ContentType
from django.db import models
from facebook_users.models import User
from m2m_history.fields import ManyToManyHistoryField

from .api import api_call
from .decorators import fetch_all, atomic
from .fields import JSONField
from .utils import get_or_create_from_small_resource, UnknownResourceType

log = logging.getLogger('facebook_api')


class OwnerableModelMixin(models.Model):

    owner_content_type = models.ForeignKey(
        ContentType, null=True, related_name='content_type_owners_%(app_label)s_%(class)ss')
    owner_id = models.BigIntegerField(null=True, db_index=True)
    owner = generic.GenericForeignKey('owner_content_type', 'owner_id')

    class Meta:
        abstract = True


class AuthorableModelMixin(models.Model):

    # object containing the name and Facebook id of the user who posted the message
    author_json = JSONField(null=True, help_text='Information about the user who posted the message')

    author_content_type = models.ForeignKey(
        ContentType, null=True, related_name='content_type_authors_%(app_label)s_%(class)ss')
    author_id = models.BigIntegerField(null=True, db_index=True)
    author = generic.GenericForeignKey('author_content_type', 'author_id')

    class Meta:
        abstract = True

    def parse(self, response):
        if 'from' in response:
            response['author_json'] = response.pop('from')

        super(AuthorableModelMixin, self).parse(response)

        if self.author is None and self.author_json:
            self.author = get_or_create_from_small_resource(self.author_json)


class ActionableModelMixin(models.Model):

    actions_count = models.PositiveIntegerField(null=True, help_text='The number of total actions with this item')

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.actions_count = sum([getattr(self, field, None) or 0
                                  for field in ['likes_count', 'shares_count', 'comments_count']])

        for reaction in ['love', 'wow', 'haha', 'sad', 'angry', 'thankful']:
            self.actions_count += getattr(self, '{0}s_count'.format(reaction), None)


class LikableModelMixin(models.Model):
    likes_users = ManyToManyHistoryField(User, related_name='like_%(class)ss')
    likes_count = models.PositiveIntegerField(null=True, help_text='The number of likes of this item')

    class Meta:
        abstract = True

    def parse(self, response):
        if 'like_count' in response:
            response['likes_count'] = response.pop('like_count')

        super(LikableModelMixin, self).parse(response)

    def update_count_and_get_like_users(self, instances, *args, **kwargs):
        self.likes_users = instances
        self.likes_count = instances.count()

        self.save()
        return instances


    # TODO: commented, becouse if many processes fetch_likes, got errors
    # DatabaseError: deadlock detected
    # DETAIL:  Process 27235 waits for ShareLock on transaction 3922627359; blocked by process 27037.
    # @atomic
    @fetch_all(return_all=update_count_and_get_like_users, paging_next_arg_name='after')
    def fetch_likes(self, limit=1000, **kwargs):
        """
        Retrieve and save all likes of post
        """
        ids = []
        response = api_call('%s/likes' % self.graph_id, limit=limit, **kwargs)
        if response:
            log.debug('response objects count=%s, limit=%s, after=%s' %
                      (len(response['data']), limit, kwargs.get('after')))
            for resource in response['data']:
                try:
                    user = get_or_create_from_small_resource(resource)
                    ids += [user.pk]
                except UnknownResourceType:
                    continue

        return User.objects.filter(pk__in=ids), response


class ReactionableModelMixin(models.Model):
    # without "Like": it may broke something
    reaction_types = ['love', 'wow', 'haha', 'sad', 'angry', 'thankful']

    reactions_count = models.PositiveIntegerField(null=True, help_text='The number of reactions of this item')

    def update_count_and_get_users_builder(reaction):

        def update_count_and_get_reaction_users(self, instances, *args, **kwargs):
            # setattr(self, '{0}s_count'.format(reaction), 0)
            setattr(self, '{0}s_users'.format(reaction), instances)
            setattr(self, '{0}s_count'.format(reaction), instances.count())

            self.save()
            return instances

        return update_count_and_get_reaction_users

    for reaction in reaction_types:
        related_name = '%s_' % reaction + '%(class)ss'
        vars()['{0}s_users'.format(reaction)] = ManyToManyHistoryField(User, related_name=related_name)
        vars()['{0}s_count'.format(reaction)] = models.PositiveIntegerField(null=True, help_text='The number of {0}s of this item'.format(reaction))

        vars()['update_count_and_get_{0}_users'.format(reaction)] = update_count_and_get_users_builder(reaction=reaction)


    class Meta:
        abstract = True

    def parse(self, response):
        for reaction in self.reaction_types:
            if '{0}_count'.format(reaction) in response:
                response['{0}s_count'.format(reaction)] = response.pop('{0}_count'.format(reaction))

        super(ReactionableModelMixin, self).parse(response)


    def fetch_reactions(self, reaction=None, limit=1000, **kwargs):
        """
        Retrieve and save all reactions of post

        Note: method may return different data structures:
            List:       if reaction is specified
            Dictionary: if reaction is not specified
        """
        ids = {}
        types = self.reaction_types + ['LIKE']
        for id_type in types:
            ids[id_type.upper()] = []

        response = api_call('%s/reactions' % self.graph_id, version=2.6, limit=limit, **kwargs)
        if response:
            log.debug('response objects count=%s, limit=%s, after=%s' %
                      (len(response['data']), limit, kwargs.get('after')))
            for resource in response['data']:
                try:
                    if (reaction != None) and (reaction.upper() != resource['type']):
                        continue
                    try:
                        user = get_or_create_from_small_resource(resource)
                        ids[resource['type']] += [user.pk]

                    except UnknownResourceType:
                        continue
                # no 'type' in resource
                except KeyError:
                    continue


        def get_user_ids(self, ids, response):
            return User.objects.filter(pk__in=ids), response

        result = {}
        for id_type in types:
            if (reaction != None) and (reaction.upper() != id_type.upper()):
                continue

            count_method = getattr(self, 'update_count_and_get_{0}_users'.format(id_type.lower()))
            # create count-and-get function wrapped in fetch_all decorator
            fetch = fetch_all(return_all=count_method, paging_next_arg_name='after')(get_user_ids)
            result[id_type.upper()] = fetch(self, ids[id_type.upper()], response)
            # for some reason fetch_all does not call count method
            count_method(result[id_type.upper()])

        if (reaction != None):
            return result[reaction.upper()]
        else:
            return result

    # separate from fetch method, because it would return wrong data if reaction specified
    def count_reactions(self):
        count = 0
        for reaction in self.reaction_types + ['like']:
            count += getattr(self, '{0}s_count'.format(reaction))

        self.reactions_count = count

        self.save()


class ShareableModelMixin(models.Model):

    shares_users = ManyToManyHistoryField(User, related_name='shares_%(class)ss')
    shares_count = models.PositiveIntegerField(null=True, help_text='The number of shares of this item')

    class Meta:
        abstract = True

    def update_count_and_get_shares_users(self, instances, *args, **kwargs):
#        self.shares_users = instances
        # becouse here are not all shares: "Some posts may not appear here because of their privacy settings."
        if self.shares_count is None:
            self.shares_count = instances.count()
            self.save()
        return instances

    @atomic
    @fetch_all(return_all=update_count_and_get_shares_users, paging_next_arg_name='after')
    def fetch_shares(self, limit=1000, **kwargs):
        """
        Retrieve and save all shares of post
        """
        from facebook_api.models import MASTER_DATABASE  # here, becouse cycling import

        ids = []

        response = api_call('%s/sharedposts' % self.graph_id, **kwargs)
        if response:
            posts = [post for post in response['data'] if post.get('from')]
            timestamps = dict([(int(post['from']['id']), datetime_parse(post['created_time'])) for post in posts])
            ids_new = timestamps.keys()
            # becouse we should use local pk, instead of remote, remove it after pk -> graph_id
            ids_current = map(int, User.objects.filter(pk__in=self.shares_users.get_query_set(
                only_pk=True).using(MASTER_DATABASE).exclude(time_from=None)).values_list('graph_id', flat=True))
            ids_add = set(ids_new).difference(set(ids_current))
            ids_add_pairs = []
            ids_remove = set(ids_current).difference(set(ids_new))

            log.debug('response objects count=%s, limit=%s, after=%s' % (len(posts), limit, kwargs.get('after')))
            for post in posts:
                graph_id = int(post['from']['id'])
                if sorted(post['from'].keys()) == ['id', 'name']:
                    try:
                        user = get_or_create_from_small_resource(post['from'])
                        ids += [user.pk]
                        # this id in add list and still not in add_pairs (sometimes in response are duplicates)
                        if graph_id in ids_add and graph_id not in map(lambda i: i[0], ids_add_pairs):
                            # becouse we should use local pk, instead of remote
                            ids_add_pairs += [(graph_id, user.pk)]
                    except UnknownResourceType:
                        continue

            m2m_model = self.shares_users.through
            # '(album|post)_id'
            field_name = [f.attname for f in m2m_model._meta.local_fields
                          if isinstance(f, models.ForeignKey) and f.name != 'user'][0]

            # remove old shares without time_from
            self.shares_users.get_query_set_through().filter(time_from=None).delete()

            # in case some ids_add already left
            self.shares_users.get_query_set_through().filter(
                **{field_name: self.pk, 'user_id__in': map(lambda i: i[1], ids_add_pairs)}).delete()

            # add new shares with specified `time_from` value
            get_share_date = lambda id: timestamps[id] if id in timestamps else self.created_time
            m2m_model.objects.bulk_create([m2m_model(
                **{field_name: self.pk, 'user_id': pk, 'time_from': get_share_date(graph_id)}) for graph_id, pk in ids_add_pairs])

        return User.objects.filter(pk__in=ids), response
