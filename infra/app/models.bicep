param name string
param location string
param tags object
param runtimePrincipalId string
param userPrincipalId string
param modelName string
param modelVersion string
param onlineCapacity int
param batchCapacity int

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: name
  location: location
  kind: 'AIServices'
  tags: tags
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: name
    disableLocalAuth: true
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: 'batch-demo'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'LLM batch automation'
    description: 'Synthetic-data batch processing demonstration.'
  }
  // The account serializes project/deployment operations; avoid concurrent child writes.
  dependsOn: [batch]
}

resource online 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'online'
  sku: { name: 'DataZoneStandard', capacity: onlineCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource batch 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'batch'
  sku: { name: 'DataZoneBatch', capacity: batchCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [online]
}

// Batch requires file/job writes that the inference-only OpenAI User role does not grant.
var inferenceRole = 'a001fd3d-188f-4b5d-821b-7da978bf7442'

resource runtimeAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, runtimePrincipalId, inferenceRole)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', inferenceRole)
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource userAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, userPrincipalId, inferenceRole)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', inferenceRole)
    principalId: userPrincipalId
    principalType: 'User'
  }
}

output endpoint string = 'https://${account.name}.openai.azure.com'
output accountName string = account.name
output projectId string = project.id
output onlineDeployment string = online.name
output batchDeployment string = batch.name
