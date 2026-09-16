@description('Location for the alert resources')
param location string

@description('Short prefix used to build resource names')
param namePrefix string

@description('Resource ID of the Log Analytics workspace the Container Apps environment logs to')
param logAnalyticsWorkspaceId string

@description('Email address to notify on alert (§7: App Insights alert rules -> email on run failure, zero-output runs, cost anomalies)')
param alertEmail string

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${namePrefix}-ops-ag'
  location: 'global'
  properties: {
    groupShortName: 'discoveryops'
    enabled: true
    emailReceivers: [
      {
        name: 'operator'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// §7: every pipeline orchestrator logs a structured marker (RUN_FAILED,
// ZERO_OUTPUT_RUN, QA_MISMATCH_RATE_EXCEEDED — see discovery.stages.*) to stdout,
// captured by the Container Apps environment into ContainerAppConsoleLogs_CL
// (confirmed against real pipeline logs, not guessed — see infra/README.md). These
// scheduled query rules watch that table for each marker. "Cost anomalies" (the
// third §7 alert category) isn't implemented here — no historical cost baseline
// exists yet to detect an anomaly against; flagged as a real gap in STATUS.md.
var alertMarkers = [
  {
    key: 'run-failed'
    displayName: 'Discovery pipeline run failed'
    searchTerm: 'RUN_FAILED'
    severity: 1
  }
  {
    key: 'zero-output'
    displayName: 'Discovery pipeline run produced zero output'
    searchTerm: 'ZERO_OUTPUT_RUN'
    severity: 2
  }
  {
    key: 'qa-mismatch'
    displayName: 'Discovery QA mismatch rate exceeded threshold'
    searchTerm: 'QA_MISMATCH_RATE_EXCEEDED'
    severity: 2
  }
]

resource alertRules 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = [
  for marker in alertMarkers: {
    name: '${namePrefix}-${marker.key}-alert'
    location: location
    properties: {
      displayName: marker.displayName
      description: 'Watches ContainerAppConsoleLogs_CL for a "${marker.searchTerm}" marker emitted by the pipeline (§7).'
      severity: marker.severity
      enabled: true
      evaluationFrequency: 'PT1H'
      windowSize: 'PT1H'
      scopes: [
        logAnalyticsWorkspaceId
      ]
      criteria: {
        allOf: [
          {
            query: 'ContainerAppConsoleLogs_CL | where Log_s contains "${marker.searchTerm}"'
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      actions: {
        actionGroups: [
          actionGroup.id
        ]
      }
    }
  }
]

output actionGroupId string = actionGroup.id
