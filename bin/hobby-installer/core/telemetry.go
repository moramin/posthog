package core

import (
	"github.com/posthog/posthog-go"
)

// Self-hosted fork: install telemetry is never sent. No client is created, so the
// install events below are no-ops and no domain is reported to anyone.
var client posthog.Client

func SendInstallStartEvent(domain string) {
	sendEvent(domain, "magic_curl_install_start")
}

func SendInstallCompleteEvent(domain string) {
	sendEvent(domain, "magic_curl_install_complete")
}

func sendEvent(domain, eventName string) {
	_, _ = domain, eventName
}

func CloseTelemetry() {
	if client != nil {
		_ = client.Close()
	}
}
