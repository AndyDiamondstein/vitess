#!/usr/bin/env python
#
# Copyright 2013, Google Inc. All rights reserved.
# Use of this source code is governed by a BSD-style license that can
# be found in the LICENSE file.

"""This test covers the workflow for a sharding merge.

We start with 3 shards: -40, 40-80, and 80-. We then merge -40 and 40-80
into -80.

Note this test is just testing the full workflow, not corner cases or error
cases. These are mostly done by the other resharding tests.
"""

import struct

import logging
import unittest

from vtdb import keyrange_constants

import base_sharding
import environment
import tablet
import utils


keyspace_id_type = keyrange_constants.KIT_UINT64
pack_keyspace_id = struct.Struct('!Q').pack

# initial shards
# shard -40
shard_0_master = tablet.Tablet()
shard_0_replica = tablet.Tablet()
shard_0_rdonly = tablet.Tablet()
# shard 40-80
shard_1_master = tablet.Tablet()
shard_1_replica = tablet.Tablet()
shard_1_rdonly = tablet.Tablet()
# shard 80-
shard_2_master = tablet.Tablet()
shard_2_replica = tablet.Tablet()
shard_2_rdonly = tablet.Tablet()

# merged shard -80
shard_dest_master = tablet.Tablet()
shard_dest_replica = tablet.Tablet()
shard_dest_rdonly = tablet.Tablet()

all_tablets = [shard_0_master, shard_0_replica, shard_0_rdonly,
               shard_1_master, shard_1_replica, shard_1_rdonly,
               shard_2_master, shard_2_replica, shard_2_rdonly,
               shard_dest_master, shard_dest_replica, shard_dest_rdonly]


def setUpModule():
  try:
    environment.topo_server().setup()
    setup_procs = [t.init_mysql() for t in all_tablets]
    utils.Vtctld().start()
    utils.wait_procs(setup_procs)
  except:
    tearDownModule()
    raise


def tearDownModule():
  utils.required_teardown()
  if utils.options.skip_teardown:
    return

  teardown_procs = [t.teardown_mysql() for t in all_tablets]
  utils.wait_procs(teardown_procs, raise_on_error=False)
  environment.topo_server().teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()
  for t in all_tablets:
    t.remove_tree()


class TestMergeSharding(unittest.TestCase, base_sharding.BaseShardingTest):

  # create_schema will create the same schema on the keyspace
  # then insert some values
  def _create_schema(self):
    if keyspace_id_type == keyrange_constants.KIT_BYTES:
      t = 'varbinary(64)'
    else:
      t = 'bigint(20) unsigned'
    create_table_template = '''create table %s(
id bigint not null,
msg varchar(64),
custom_sharding_key ''' + t + ''' not null,
primary key (id),
index by_msg (msg)
) Engine=InnoDB'''
    create_view_template = (
        'create view %s'
        '(id, msg, custom_sharding_key) as select id, msg, custom_sharding_key '
        'from %s')

    utils.run_vtctl(['ApplySchema',
                     '-sql=' + create_table_template % ('resharding1'),
                     'test_keyspace'],
                    auto_log=True)
    utils.run_vtctl(['ApplySchema',
                     '-sql=' + create_table_template % ('resharding2'),
                     'test_keyspace'],
                    auto_log=True)
    utils.run_vtctl(['ApplySchema',
                     '-sql=' + create_view_template % ('view1', 'resharding1'),
                     'test_keyspace'],
                    auto_log=True)

  # _insert_value inserts a value in the MySQL database along with the comments
  # required for routing.
  def _insert_value(self, tablet_obj, table, mid, msg, custom_sharding_key):
    k = utils.uint64_to_hex(custom_sharding_key)
    tablet_obj.mquery(
        'vt_test_keyspace',
        ['begin',
         'insert into %s(id, msg, custom_sharding_key) '
         'values(%d, "%s", 0x%x) /* vtgate:: keyspace_id:%s */ '
         '/* id:%d */' %
         (table, mid, msg, custom_sharding_key, k, mid),
         'commit'],
        write=True)

  def _get_value(self, tablet_obj, table, mid):
    """Returns the row(s) from the table for the provided id, using MySQL.

    Args:
      tablet_obj: the tablet to get data from.
      table: the table to query.
      mid: id field of the table.
    Returns:
      A tuple of results.
    """
    return tablet_obj.mquery(
        'vt_test_keyspace',
        'select id, msg, custom_sharding_key from %s where id=%d' %
        (table, mid))

  def _check_value(self, tablet_obj, table, mid, msg, custom_sharding_key,
                   should_be_here=True):
    result = self._get_value(tablet_obj, table, mid)
    if keyspace_id_type == keyrange_constants.KIT_BYTES:
      fmt = '%s'
      custom_sharding_key = pack_keyspace_id(custom_sharding_key)
    else:
      fmt = '%x'
    if should_be_here:
      self.assertEqual(result, ((mid, msg, custom_sharding_key),),
                       ('Bad row in tablet %s for id=%d, custom_sharding_key=' +
                        fmt + ', row=%s') % (tablet_obj.tablet_alias, mid,
                                             custom_sharding_key, str(result)))
    else:
      self.assertEqual(
          len(result), 0,
          ('Extra row in tablet %s for id=%d, custom_sharding_key=' +
           fmt + ': %s') % (tablet_obj.tablet_alias, mid, custom_sharding_key,
                            str(result)))

  # _is_value_present_and_correct tries to read a value.
  # if it is there, it will check it is correct and return True if it is.
  # if not correct, it will self.fail.
  # if not there, it will return False.
  def _is_value_present_and_correct(
      self, tablet_obj, table, mid, msg, custom_sharding_key):
    result = self._get_value(tablet_obj, table, mid)
    if not result:
      return False
    if keyspace_id_type == keyrange_constants.KIT_BYTES:
      fmt = '%s'
      custom_sharding_key = pack_keyspace_id(custom_sharding_key)
    else:
      fmt = '%x'
    self.assertEqual(result, ((mid, msg, custom_sharding_key),),
                     ('Bad row in tablet %s for id=%d, '
                      'custom_sharding_key=' + fmt) % (
                          tablet_obj.tablet_alias, mid, custom_sharding_key))
    return True

  def _insert_startup_values(self):
    self._insert_value(shard_0_master, 'resharding1', 0, 'msg1',
                       0x1000000000000000)
    self._insert_value(shard_1_master, 'resharding1', 1, 'msg2',
                       0x5000000000000000)
    self._insert_value(shard_2_master, 'resharding1', 2, 'msg3',
                       0xD000000000000000)

  def _check_startup_values(self):
    # check first two values are in the right shard
    self._check_value(shard_dest_master, 'resharding1', 0, 'msg1',
                      0x1000000000000000)
    self._check_value(shard_dest_replica, 'resharding1', 0, 'msg1',
                      0x1000000000000000)
    self._check_value(shard_dest_rdonly, 'resharding1', 0, 'msg1',
                      0x1000000000000000)

    self._check_value(shard_dest_master, 'resharding1', 1, 'msg2',
                      0x5000000000000000)
    self._check_value(shard_dest_replica, 'resharding1', 1, 'msg2',
                      0x5000000000000000)
    self._check_value(shard_dest_rdonly, 'resharding1', 1, 'msg2',
                      0x5000000000000000)

  def _insert_lots(self, count, base=0):
    if count > 10000:
      self.assertFail('bad count passed in, only support up to 10000')
    for i in xrange(count):
      self._insert_value(shard_0_master, 'resharding1', 1000000 + base + i,
                         'msg-range0-%d' % i, 0x2000000000000000 + base + i)
      self._insert_value(shard_1_master, 'resharding1', 1010000 + base + i,
                         'msg-range1-%d' % i, 0x6000000000000000 + base + i)

  # _check_lots returns how many of the values we have, in percents.
  def _check_lots(self, count, base=0):
    found = 0
    for i in xrange(count):
      if self._is_value_present_and_correct(shard_dest_replica, 'resharding1',
                                            1000000 + base + i,
                                            'msg-range0-%d' % i,
                                            0x2000000000000000 + base + i):
        found += 1
      if self._is_value_present_and_correct(shard_dest_replica, 'resharding1',
                                            1010000 + base + i,
                                            'msg-range1-%d' % i,
                                            0x6000000000000000 + base + i):
        found += 1
    percent = found * 100 / count / 2
    logging.debug('I have %d%% of the data', percent)
    return percent

  def _check_lots_timeout(self, count, threshold, timeout, base=0):
    while True:
      value = self._check_lots(count, base=base)
      if value >= threshold:
        return value
      timeout = utils.wait_step('waiting for %d%% of the data' % threshold,
                                timeout, sleep_time=1)

  def test_merge_sharding(self):
    utils.run_vtctl(['CreateKeyspace',
                     '--sharding_column_name', 'custom_sharding_key',
                     '--sharding_column_type', keyspace_id_type,
                     '--split_shard_count', '4',
                     'test_keyspace'])

    shard_0_master.init_tablet('master', 'test_keyspace', '-40')
    shard_0_replica.init_tablet('replica', 'test_keyspace', '-40')
    shard_0_rdonly.init_tablet('rdonly', 'test_keyspace', '-40')
    shard_1_master.init_tablet('master', 'test_keyspace', '40-80')
    shard_1_replica.init_tablet('replica', 'test_keyspace', '40-80')
    shard_1_rdonly.init_tablet('rdonly', 'test_keyspace', '40-80')
    shard_2_master.init_tablet('master', 'test_keyspace', '80-')
    shard_2_replica.init_tablet('replica', 'test_keyspace', '80-')
    shard_2_rdonly.init_tablet('rdonly', 'test_keyspace', '80-')

    utils.run_vtctl(['RebuildKeyspaceGraph', 'test_keyspace'], auto_log=True)

    ks = utils.run_vtctl_json(['GetSrvKeyspace', 'test_nj', 'test_keyspace'])
    self.assertEqual(ks['split_shard_count'], 4)

    # create databases so vttablet can start behaving normally
    for t in [shard_0_master, shard_0_replica, shard_0_rdonly,
              shard_1_master, shard_1_replica, shard_1_rdonly,
              shard_2_master, shard_2_replica, shard_2_rdonly]:
      t.create_db('vt_test_keyspace')
      t.start_vttablet(wait_for_state=None)

    for t in [shard_0_master, shard_0_replica, shard_0_rdonly,
              shard_1_master, shard_1_replica, shard_1_rdonly,
              shard_2_master, shard_2_replica, shard_2_rdonly]:
      t.wait_for_vttablet_state('SERVING')

    # reparent to make the tablets work
    utils.run_vtctl(['InitShardMaster', 'test_keyspace/-40',
                     shard_0_master.tablet_alias], auto_log=True)
    utils.run_vtctl(['InitShardMaster', 'test_keyspace/40-80',
                     shard_1_master.tablet_alias], auto_log=True)
    utils.run_vtctl(['InitShardMaster', 'test_keyspace/80-',
                     shard_2_master.tablet_alias], auto_log=True)

    # create the tables
    self._create_schema()
    self._insert_startup_values()

    # run a health check on source replicas so they respond to discovery
    # (for binlog players) and on the source rdonlys (for workers)
    for t in [shard_0_replica, shard_1_replica]:
      utils.run_vtctl(['RunHealthCheck', t.tablet_alias, 'replica'])
    for t in [shard_0_rdonly, shard_1_rdonly]:
      utils.run_vtctl(['RunHealthCheck', t.tablet_alias, 'rdonly'])

    # create the merge shards
    shard_dest_master.init_tablet('master', 'test_keyspace', '-80')
    shard_dest_replica.init_tablet('replica', 'test_keyspace', '-80')
    shard_dest_rdonly.init_tablet('rdonly', 'test_keyspace', '-80')

    # start vttablet on the split shards (no db created,
    # so they're all not serving)
    for t in [shard_dest_master, shard_dest_replica, shard_dest_rdonly]:
      t.start_vttablet(wait_for_state=None)
    for t in [shard_dest_master, shard_dest_replica, shard_dest_rdonly]:
      t.wait_for_vttablet_state('NOT_SERVING')

    utils.run_vtctl(['InitShardMaster', 'test_keyspace/-80',
                     shard_dest_master.tablet_alias], auto_log=True)

    utils.run_vtctl(['RebuildKeyspaceGraph', 'test_keyspace'],
                    auto_log=True)
    utils.check_srv_keyspace(
        'test_nj', 'test_keyspace',
        'Partitions(master): -40 40-80 80-\n'
        'Partitions(rdonly): -40 40-80 80-\n'
        'Partitions(replica): -40 40-80 80-\n',
        keyspace_id_type=keyspace_id_type,
        sharding_column_name='custom_sharding_key')

    # copy the schema
    utils.run_vtctl(['CopySchemaShard', shard_0_rdonly.tablet_alias,
                     'test_keyspace/-80'], auto_log=True)

    # copy the data (will also start filtered replication), reset source
    utils.run_vtworker(['--cell', 'test_nj',
                        '--command_display_interval', '10ms',
                        'SplitClone',
                        '--source_reader_count', '10',
                        '--min_table_size_for_split', '1',
                        '--min_healthy_rdonly_endpoints', '1',
                        'test_keyspace/-80'],
                       auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_0_rdonly.tablet_alias,
                     'rdonly'], auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_1_rdonly.tablet_alias,
                     'rdonly'], auto_log=True)

    # check the startup values are in the right place
    self._check_startup_values()

    # check the schema too
    utils.run_vtctl(['ValidateSchemaKeyspace', 'test_keyspace'], auto_log=True)

    # check binlog player variables
    self.check_destination_master(shard_dest_master,
                                  ['test_keyspace/-40', 'test_keyspace/40-80'])

    # check that binlog server exported the stats vars
    self.check_binlog_server_vars(shard_0_replica, horizontal=True)
    self.check_binlog_server_vars(shard_1_replica, horizontal=True)

    # testing filtered replication: insert a bunch of data on shard 0 and 1,
    # check we get most of it after a few seconds, wait for binlog server
    # timeout, check we get all of it.
    logging.debug('Inserting lots of data on source shards')
    self._insert_lots(1000)
    logging.debug('Checking 80 percent of data is sent quickly')
    v = self._check_lots_timeout(1000, 80, 10)
    if v != 100:
      # small optimization: only do this check if we don't have all the data
      # already anyway.
      logging.debug('Checking all data goes through eventually')
      self._check_lots_timeout(1000, 100, 30)
    self.check_binlog_player_vars(shard_dest_master,
                                  ['test_keyspace/-40', 'test_keyspace/40-80'],
                                  seconds_behind_master_max=30)
    self.check_binlog_server_vars(shard_0_replica, horizontal=True,
                                  min_statements=1000, min_transactions=1000)
    self.check_binlog_server_vars(shard_1_replica, horizontal=True,
                                  min_statements=1000, min_transactions=1000)

    # use vtworker to compare the data (after health-checking the destination
    # rdonly tablets so discovery works)
    utils.run_vtctl(['RunHealthCheck', shard_dest_rdonly.tablet_alias,
                     'rdonly'])
    logging.debug('Running vtworker SplitDiff on first half')
    utils.run_vtworker(['-cell', 'test_nj', 'SplitDiff',
                        '--exclude_tables', 'unrelated',
                        '--min_healthy_rdonly_endpoints', '1',
                        '--source_uid', '0',
                        'test_keyspace/-80'],
                       auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_0_rdonly.tablet_alias, 'rdonly'],
                    auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_dest_rdonly.tablet_alias,
                     'rdonly'], auto_log=True)
    logging.debug('Running vtworker SplitDiff on second half')
    utils.run_vtworker(['-cell', 'test_nj', 'SplitDiff',
                        '--exclude_tables', 'unrelated',
                        '--min_healthy_rdonly_endpoints', '1',
                        '--source_uid', '1',
                        'test_keyspace/-80'],
                       auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_1_rdonly.tablet_alias, 'rdonly'],
                    auto_log=True)
    utils.run_vtctl(['ChangeSlaveType', shard_dest_rdonly.tablet_alias,
                     'rdonly'], auto_log=True)

    # get status for the destination master tablet, make sure we have it all
    self.check_running_binlog_player(shard_dest_master, 3000, 1000)

    # check destination master query service is not running
    utils.check_tablet_query_service(self, shard_dest_master, False, False)
    stream_health = utils.run_vtctl_json(['VtTabletStreamHealth',
                                          '-count', '1',
                                          shard_dest_master.tablet_alias])
    logging.debug('Got health: %s', str(stream_health))
    self.assertIn('realtime_stats', stream_health)
    self.assertNotIn('serving', stream_health)

    # check the destination master 3 is healthy, even though its query
    # service is not running (if not healthy this would exception out)
    shard_dest_master.get_healthz()

    # now serve rdonly from the split shards
    utils.run_vtctl(['MigrateServedTypes', 'test_keyspace/-80', 'rdonly'],
                    auto_log=True)
    utils.check_srv_keyspace('test_nj', 'test_keyspace',
                             'Partitions(master): -40 40-80 80-\n'
                             'Partitions(rdonly): -80 80-\n'
                             'Partitions(replica): -40 40-80 80-\n',
                             keyspace_id_type=keyspace_id_type,
                             sharding_column_name='custom_sharding_key')

    # now serve replica from the split shards
    utils.run_vtctl(['MigrateServedTypes', 'test_keyspace/-80', 'replica'],
                    auto_log=True)
    utils.check_srv_keyspace('test_nj', 'test_keyspace',
                             'Partitions(master): -40 40-80 80-\n'
                             'Partitions(rdonly): -80 80-\n'
                             'Partitions(replica): -80 80-\n',
                             keyspace_id_type=keyspace_id_type,
                             sharding_column_name='custom_sharding_key')

    # now serve master from the split shards
    utils.run_vtctl(['MigrateServedTypes', 'test_keyspace/-80', 'master'],
                    auto_log=True)
    utils.check_srv_keyspace('test_nj', 'test_keyspace',
                             'Partitions(master): -80 80-\n'
                             'Partitions(rdonly): -80 80-\n'
                             'Partitions(replica): -80 80-\n',
                             keyspace_id_type=keyspace_id_type,
                             sharding_column_name='custom_sharding_key')
    utils.check_tablet_query_service(self, shard_0_master, False, True)
    utils.check_tablet_query_service(self, shard_1_master, False, True)

    # check the binlog players are gone now
    self.check_no_binlog_player(shard_dest_master)

    # kill the original tablets in the original shards
    tablet.kill_tablets([shard_0_master, shard_0_replica, shard_0_rdonly,
                         shard_1_master, shard_1_replica, shard_1_rdonly])
    for t in [shard_0_replica, shard_0_rdonly,
              shard_1_replica, shard_1_rdonly]:
      utils.run_vtctl(['DeleteTablet', t.tablet_alias], auto_log=True)
    for t in [shard_0_master, shard_1_master]:
      utils.run_vtctl(['DeleteTablet', '-allow_master', t.tablet_alias],
                      auto_log=True)

    # delete the original shards
    utils.run_vtctl(['DeleteShard', 'test_keyspace/-40'], auto_log=True)
    utils.run_vtctl(['DeleteShard', 'test_keyspace/40-80'], auto_log=True)

    # rebuild the serving graph, all mentions of the old shards shoud be gone
    utils.run_vtctl(['RebuildKeyspaceGraph', 'test_keyspace'], auto_log=True)

    # kill everything else
    tablet.kill_tablets([shard_2_master, shard_2_replica, shard_2_rdonly,
                         shard_dest_master, shard_dest_replica,
                         shard_dest_rdonly])


if __name__ == '__main__':
  utils.main()
