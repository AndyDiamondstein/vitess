// Copyright 2012, Google Inc. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package tabletserver

import (
	"encoding/json"
	"fmt"
	"html/template"
	"net/url"
	"strings"
	"time"

	log "github.com/golang/glog"
	"github.com/youtube/vitess/go/sqltypes"
	"github.com/youtube/vitess/go/streamlog"
	"github.com/youtube/vitess/go/vt/callerid"
	"github.com/youtube/vitess/go/vt/callinfo"
	"golang.org/x/net/context"
)

// StatsLogger is the main stream logger object
var StatsLogger = streamlog.New("TabletServer", 50)

const (
	// QuerySourceRowcache means query result is found in rowcache.
	QuerySourceRowcache = 1 << iota
	// QuerySourceConsolidator means query result is found in consolidator.
	QuerySourceConsolidator
	// QuerySourceMySQL means query result is returned from MySQL.
	QuerySourceMySQL
)

// LogStats records the stats for a single query
type LogStats struct {
	Method               string
	PlanType             string
	OriginalSQL          string
	BindVariables        map[string]interface{}
	rewrittenSqls        []string
	RowsAffected         int
	NumberOfQueries      int
	StartTime            time.Time
	EndTime              time.Time
	MysqlResponseTime    time.Duration
	WaitingForConnection time.Duration
	CacheHits            int64
	CacheAbsent          int64
	CacheMisses          int64
	CacheInvalidations   int64
	QuerySources         byte
	Rows                 [][]sqltypes.Value
	TransactionID        int64
	ctx                  context.Context
	Error                *TabletError
}

func newLogStats(methodName string, ctx context.Context) *LogStats {
	return &LogStats{
		Method:    methodName,
		StartTime: time.Now(),
		ctx:       ctx,
	}
}

// Send finalizes a record and sends it
func (stats *LogStats) Send() {
	stats.EndTime = time.Now()
	StatsLogger.Send(stats)
}

// ImmediateCaller returns the immediate caller stored in LogStats.ctx
func (stats *LogStats) ImmediateCaller() string {
	return callerid.GetUsername(callerid.ImmediateCallerIDFromContext(stats.ctx))
}

// EffectiveCaller returns the effective caller stored in LogStats.ctx
func (stats *LogStats) EffectiveCaller() string {
	return callerid.GetPrincipal(callerid.EffectiveCallerIDFromContext(stats.ctx))
}

// EventTime returns the time the event was created.
func (stats *LogStats) EventTime() time.Time {
	return stats.EndTime
}

// AddRewrittenSQL adds a single sql statement to the rewritten list
func (stats *LogStats) AddRewrittenSQL(sql string, start time.Time) {
	stats.QuerySources |= QuerySourceMySQL
	stats.NumberOfQueries++
	stats.rewrittenSqls = append(stats.rewrittenSqls, sql)
	stats.MysqlResponseTime += time.Now().Sub(start)
}

// TotalTime returns how long this query has been running
func (stats *LogStats) TotalTime() time.Duration {
	return stats.EndTime.Sub(stats.StartTime)
}

// RewrittenSQL returns a semicolon separated list of SQL statements
// that were executed.
func (stats *LogStats) RewrittenSQL() string {
	return strings.Join(stats.rewrittenSqls, "; ")
}

// SizeOfResponse returns the approximate size of the response in
// bytes (this does not take in account protocol encoding). It will return
// 0 for streaming requests.
func (stats *LogStats) SizeOfResponse() int {
	if stats.Rows == nil {
		return 0
	}
	size := 0
	for _, row := range stats.Rows {
		for _, field := range row {
			size += field.Len()
		}
	}
	return size
}

// FmtBindVariables returns the map of bind variables as JSON. For
// values that are strings or byte slices it only reports their type
// and length.
func (stats *LogStats) FmtBindVariables(full bool) string {
	var out map[string]interface{}
	if full {
		out = stats.BindVariables
	} else {
		// NOTE(szopa): I am getting rid of potentially large bind
		// variables.
		out = make(map[string]interface{})
		for k, v := range stats.BindVariables {
			switch val := v.(type) {
			case string:
				out[k] = fmt.Sprintf("string %v", len(val))
			case []byte:
				out[k] = fmt.Sprintf("bytes %v", len(val))
			default:
				out[k] = v
			}
		}
	}
	b, err := json.Marshal(out)
	if err != nil {
		log.Warningf("could not marshal %q", stats.BindVariables)
		return ""
	}
	return string(b)
}

// FmtQuerySources returns a comma separated list of query
// sources. If there were no query sources, it returns the string
// "none".
func (stats *LogStats) FmtQuerySources() string {
	if stats.QuerySources == 0 {
		return "none"
	}
	sources := make([]string, 3)
	n := 0
	if stats.QuerySources&QuerySourceMySQL != 0 {
		sources[n] = "mysql"
		n++
	}
	if stats.QuerySources&QuerySourceRowcache != 0 {
		sources[n] = "rowcache"
		n++
	}
	if stats.QuerySources&QuerySourceConsolidator != 0 {
		sources[n] = "consolidator"
		n++
	}
	return strings.Join(sources[:n], ",")
}

// ContextHTML returns the HTML version of the context that was used, or "".
// This is a method on LogStats instead of a field so that it doesn't need
// to be passed by value everywhere.
func (stats *LogStats) ContextHTML() template.HTML {
	return callinfo.HTMLFromContext(stats.ctx)
}

// ErrorStr returns the error string or ""
func (stats *LogStats) ErrorStr() string {
	if stats.Error != nil {
		return stats.Error.Error()
	}
	return ""
}

// RemoteAddrUsername returns some parts of CallInfo if set
func (stats *LogStats) RemoteAddrUsername() (string, string) {
	ci, ok := callinfo.FromContext(stats.ctx)
	if !ok {
		return "", ""
	}
	return ci.RemoteAddr(), ci.Username()
}

// Format returns a tab separated list of logged fields.
func (stats *LogStats) Format(params url.Values) string {
	_, fullBindParams := params["full"]

	// TODO: remove username here we fully enforce immediate caller id
	remoteAddr, username := stats.RemoteAddrUsername()
	return fmt.Sprintf(
		"%v\t%v\t%v\t'%v'\t'%v'\t%v\t%v\t%.6f\t%v\t%q\t%v\t%v\t%q\t%v\t%.6f\t%.6f\t%v\t%v\t%v\t%v\t%v\t%v\t%q\t\n",
		stats.Method,
		remoteAddr,
		username,
		stats.ImmediateCaller(),
		stats.EffectiveCaller(),
		stats.StartTime.Format(time.StampMicro),
		stats.EndTime.Format(time.StampMicro),
		stats.TotalTime().Seconds(),
		stats.PlanType,
		stats.OriginalSQL,
		stats.FmtBindVariables(fullBindParams),
		stats.NumberOfQueries,
		stats.RewrittenSQL(),
		stats.FmtQuerySources(),
		stats.MysqlResponseTime.Seconds(),
		stats.WaitingForConnection.Seconds(),
		stats.RowsAffected,
		stats.SizeOfResponse(),
		stats.CacheHits,
		stats.CacheMisses,
		stats.CacheAbsent,
		stats.CacheInvalidations,
		stats.ErrorStr(),
	)
}
