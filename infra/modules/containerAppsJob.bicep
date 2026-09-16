@description('Location for the Container Apps Job')
param location string

@description('Short prefix used to build resource names')
param namePrefix string

@description('Resource ID of the Container Apps environment to run the job in')
param containerAppsEnvironmentId string

@description('Cron expression for the scheduled trigger (standard 5-field cron, UTC)')
param cronExpression string = '0 6 1 * *'

@description('Name of the Key Vault the job identity should be able to read secrets from')
param keyVaultName string

var keyVaultSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource keyVaultExisting 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// G1.1 acceptance is "hello-world job runs on schedule" — this placeholder image
// is swapped for the real pipeline image (built from /pipeline) in G1.2.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-job-identity'
  location: location
}

resource job 'Microsoft.App/jobs@2023-05-01' = {
  name: '${namePrefix}-pipeline-job'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 600
      replicaRetryLimit: 1
    }
    template: {
      containers: [
        {
          name: 'hello-world'
          image: 'mcr.microsoft.com/azure-cli:latest'
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'echo "Discovery pipeline job placeholder — G1.1 acceptance test" && date'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
}

// Job identity can read secrets (Anthropic key, DB connection string) — created here
// rather than in main.bicep because a role-assignment name/scope must be resolvable
// from a same-file resource, not a cross-module output.
resource kvRoleAssignmentJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultExisting.id, identity.id, 'kv-secrets-user')
  scope: keyVaultExisting
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output identityPrincipalId string = identity.properties.principalId
output jobName string = job.name
