@description('Location for the Container Apps Job')
param location string

@description('Short prefix used to build resource names')
param namePrefix string

@description('Resource ID of the Container Apps environment to run the job in')
param containerAppsEnvironmentId string

@description('Cron expression for the scheduled trigger (standard 5-field cron, UTC) — monthly, matching the BMF publish cadence (§4 Stage 0)')
param cronExpression string = '0 6 1 * *'

@description('Name of the Key Vault the job identity should be able to read secrets from')
param keyVaultName string

@description('Login server of the ACR the job identity should be able to pull from')
param acrLoginServer string

@description('Name of the ACR (for the AcrPull role assignment)')
param acrName string

@description('Pipeline container image, including tag, e.g. adiscdevacr.azurecr.io/discovery-pipeline:latest')
param image string

var keyVaultSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource keyVaultExisting 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource acrExisting 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

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
      replicaTimeout: 3600
      replicaRetryLimit: 1
      registries: [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: '${keyVaultExisting.properties.vaultUri}secrets/db-connection-string'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'pipeline'
          image: image
          env: [
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
        }
      ]
    }
  }
}

// Job identity can read secrets (Anthropic key, DB connection string) and pull images —
// created here rather than in main.bicep because a role-assignment name/scope must be
// resolvable from a same-file resource, not a cross-module output.
resource kvRoleAssignmentJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultExisting.id, identity.id, 'kv-secrets-user')
  scope: keyVaultExisting
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrRoleAssignmentJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrExisting.id, identity.id, 'acr-pull')
  scope: acrExisting
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output identityPrincipalId string = identity.properties.principalId
output jobName string = job.name
