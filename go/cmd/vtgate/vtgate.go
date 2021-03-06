// Copyright 2012, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package main

import (
	"flag"
	"math/rand"
	"strings"
	"time"

	"golang.org/x/net/context"

	log "github.com/golang/glog"

	"github.com/youtube/vitess/go/exit"
	"github.com/youtube/vitess/go/vt/discovery"
	"github.com/youtube/vitess/go/vt/servenv"
	"github.com/youtube/vitess/go/vt/topo"
	"github.com/youtube/vitess/go/vt/topo/topoproto"
	"github.com/youtube/vitess/go/vt/vtgate"

	topodatapb "github.com/youtube/vitess/go/vt/proto/topodata"
)

var (
	cell                  = flag.String("cell", "test_nj", "cell to use")
	retryDelay            = flag.Duration("retry-delay", 2*time.Millisecond, "retry delay")
	retryCount            = flag.Int("retry-count", 2, "retry count")
	connTimeoutTotal      = flag.Duration("conn-timeout-total", 3*time.Second, "vttablet connection timeout (total)")
	connTimeoutPerConn    = flag.Duration("conn-timeout-per-conn", 1500*time.Millisecond, "vttablet connection timeout (per connection)")
	connLife              = flag.Duration("conn-life", 365*24*time.Hour, "average life of vttablet connections")
	maxInFlight           = flag.Int("max-in-flight", 0, "maximum number of calls to allow simultaneously")
	healthCheckRetryDelay = flag.Duration("healthcheck_retry_delay", 2*time.Millisecond, "health check retry delay")
	healthCheckTimeout    = flag.Duration("healthcheck_timeout", time.Minute, "the health check timeout period")
	tabletTypesToWait     = flag.String("tablet_types_to_wait", "", "wait till connected for specified tablet types during Gateway initialization")
	testGateway           = flag.String("test_gateway", "", "additional gateway to test health check module")
)

var resilientSrvTopoServer *vtgate.ResilientSrvTopoServer
var healthCheck discovery.HealthCheck

var initFakeZK func()

func init() {
	rand.Seed(time.Now().UnixNano())
	servenv.RegisterDefaultFlags()
}

func main() {
	defer exit.Recover()

	flag.Parse()
	servenv.Init()

	if initFakeZK != nil {
		initFakeZK()
	}
	ts := topo.GetServer()
	defer topo.CloseServers()

	resilientSrvTopoServer = vtgate.NewResilientSrvTopoServer(ts, "ResilientSrvTopoServer")

	healthCheck = discovery.NewHealthCheck(*connTimeoutTotal, *healthCheckRetryDelay, *healthCheckTimeout, "" /* statsSuffix */)

	tabletTypes := make([]topodatapb.TabletType, 0, 1)
	if len(*tabletTypesToWait) != 0 {
		for _, ttStr := range strings.Split(*tabletTypesToWait, ",") {
			tt, err := topoproto.ParseTabletType(ttStr)
			if err != nil {
				log.Errorf("unknown tablet type: %v", ttStr)
				continue
			}
			tabletTypes = append(tabletTypes, tt)
		}
	}
	vtg := vtgate.Init(context.Background(), healthCheck, ts, resilientSrvTopoServer, *cell, *retryDelay, *retryCount, *connTimeoutTotal, *connTimeoutPerConn, *connLife, tabletTypes, *maxInFlight, *testGateway)

	servenv.OnRun(func() {
		addStatusParts(vtg)
	})
	servenv.RunDefault()
}
