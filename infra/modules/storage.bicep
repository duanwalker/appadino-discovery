@description('Location for the storage account')
param location string

@description('Short prefix used to build resource names')
param namePrefix string

@description('Name of the Container Apps environment to register the archive-cache file share with')
param containerAppsEnvironmentName string

@description('Name of the Azure Files share used to cache IRS 990 bulk-download archives')
param archiveCacheShareName string = 'irs-archive-cache'

@description('Quota (GiB) for the archive cache share — headroom over the ~11GB rolling 3-year working set sized 2026-09-22 (see STATUS.md)')
param archiveCacheShareQuotaGb int = 200

// Storage account names: 3-24 chars, lowercase letters+digits only, globally unique —
// same constraint acr.bicep already works around for adiscdevacr.
var storageAccountName = '${replace(namePrefix, '-', '')}archive'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource archiveShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: archiveCacheShareName
  properties: {
    shareQuota: archiveCacheShareQuotaGb
  }
}

resource containerAppsEnvExisting 'Microsoft.App/managedEnvironments@2023-05-01' existing = {
  name: containerAppsEnvironmentName
}

// Registers the share with the Container Apps environment so containerAppsJob.bicep's
// volumes[].storageName can reference it by this resource's name. Uses the account key
// (not identity-based mount) — Azure Files volume mounts on Container Apps don't support
// user-assigned-identity auth as of API version 2023-05-01, unlike the KV/ACR access
// elsewhere in this stack. The key itself is never a module output (avoids landing it in
// deployment history) — it's read and used only within this resource declaration.
resource archiveCacheEnvStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  parent: containerAppsEnvExisting
  name: 'archive-cache'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: archiveShare.name
      accessMode: 'ReadWrite'
    }
  }
}

output storageAccountName string = storageAccount.name
output fileShareName string = archiveShare.name
output envStorageName string = archiveCacheEnvStorage.name
