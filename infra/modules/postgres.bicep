@description('Location for the PostgreSQL server')
param location string

@description('Short prefix used to build resource names')
param namePrefix string

@description('PostgreSQL administrator login name')
param administratorLogin string

@secure()
@description('PostgreSQL administrator password')
param administratorLoginPassword string

@description('Name of the application database to create')
param databaseName string = 'discovery'

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: '${namePrefix}-pg'
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorLoginPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: postgres
  name: databaseName
}

// Phase 1 has no VNet; the pipeline job and any local admin access come from
// Azure-hosted compute or developer IPs added ad hoc via `az postgres flexible-server firewall-rule create`.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

output fqdn string = postgres.properties.fullyQualifiedDomainName
output serverName string = postgres.name
output databaseName string = databaseName
