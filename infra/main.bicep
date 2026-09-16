targetScope = 'resourceGroup'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Short prefix used to build resource names (keep low-cardinality: lowercase, hyphens)')
param namePrefix string = 'adisc-dev'

@description('PostgreSQL administrator login')
param postgresAdminLogin string

@secure()
@description('PostgreSQL administrator password — pass at deploy time, never commit')
param postgresAdminPassword string

@description('Azure AD object ID of the operator who should be able to manage Key Vault secrets (e.g. add the Anthropic API key)')
param keyVaultAdminPrincipalId string

@description('Tag of the discovery-pipeline image to run in the Container Apps Job')
param pipelineImageTag string = 'latest'

var keyVaultSecretsOfficerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
// Computed directly (not from the module output) so it's resolvable at the start of
// deployment — required for use in role-assignment name/scope and secret parent below.
var keyVaultName = '${namePrefix}-kv'
// ACR names can't contain hyphens.
var acrNamePrefix = replace(namePrefix, '-', '')

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    namePrefix: namePrefix
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyVault'
  params: {
    location: location
    namePrefix: namePrefix
  }
}

module containerAppsEnv 'modules/containerAppsEnv.bicep' = {
  name: 'containerAppsEnv'
  params: {
    location: location
    namePrefix: namePrefix
  }
}

module appInsights 'modules/appInsights.bicep' = {
  name: 'appInsights'
  params: {
    location: location
    namePrefix: namePrefix
    logAnalyticsWorkspaceId: containerAppsEnv.outputs.logAnalyticsId
  }
}

module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    location: location
    namePrefix: acrNamePrefix
  }
}

module containerAppsJob 'modules/containerAppsJob.bicep' = {
  name: 'containerAppsJob'
  params: {
    location: location
    namePrefix: namePrefix
    containerAppsEnvironmentId: containerAppsEnv.outputs.id
    keyVaultName: keyVaultName
    acrLoginServer: acr.outputs.loginServer
    acrName: acr.outputs.name
    image: '${acr.outputs.loginServer}/discovery-pipeline:${pipelineImageTag}'
  }
  dependsOn: [
    keyVault
  ]
}

resource keyVaultExisting 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
  dependsOn: [
    keyVault
  ]
}

// Operator can manage secrets (add appadino-discoveryAI-key [Anthropic], fullenrich-api-key later)
resource kvRoleAssignmentAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultExisting.id, keyVaultAdminPrincipalId, 'kv-secrets-officer')
  scope: keyVaultExisting
  properties: {
    roleDefinitionId: keyVaultSecretsOfficerRoleId
    principalId: keyVaultAdminPrincipalId
    principalType: 'User'
  }
}

resource dbConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVaultExisting
  name: 'db-connection-string'
  properties: {
    value: 'postgresql://${postgresAdminLogin}:${postgresAdminPassword}@${postgres.outputs.fqdn}:5432/${postgres.outputs.databaseName}?sslmode=require'
  }
  dependsOn: [
    kvRoleAssignmentAdmin
  ]
}

output postgresFqdn string = postgres.outputs.fqdn
output deployedKeyVaultName string = keyVault.outputs.name
output containerAppsJobName string = containerAppsJob.outputs.jobName
output appInsightsConnectionString string = appInsights.outputs.connectionString
output acrLoginServer string = acr.outputs.loginServer
