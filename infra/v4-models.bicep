targetScope = 'resourceGroup'

@description('Existing approved AI Services account in a US region. The control deployment is not modified.')
param accountName string

@description('Names must match the v4 runtime deployment map.')
@minLength(3)
@maxLength(3)
param deploymentNames array = [
  'gpt5-mini-v4-1'
  'gpt5-mini-v4-2'
  'gpt5-mini-v4-3'
]

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

@batchSize(1)
resource pool 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for name in deploymentNames: {
  parent: account
  name: name
  sku: {
    name: 'DataZoneStandard'
    capacity: 50
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]

output deployments array = [for (name, index) in deploymentNames: {
  name: name
  id: pool[index].id
  capacity: 50
}]
