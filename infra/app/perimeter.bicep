param name string
param location string
param tags object
param storageResourceId string
param workspaceResourceId string = ''
@description('A specific approved client address in CIDR format, normally a public IPv4 /32.')
param clientAddressPrefix string = ''

resource perimeter 'Microsoft.Network/networkSecurityPerimeters@2024-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {}
}

resource profile 'Microsoft.Network/networkSecurityPerimeters/profiles@2024-07-01' = {
  parent: perimeter
  name: 'demo'
  properties: {}
}

resource subscriptionRule 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2024-07-01' = {
  parent: profile
  name: 'demo-subscription-identities'
  properties: {
    direction: 'Inbound'
    subscriptions: [
      { id: subscription().id }
    ]
  }
}

resource clientRule 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2024-07-01' = if (!empty(clientAddressPrefix)) {
  parent: profile
  name: 'approved-test-client'
  properties: {
    direction: 'Inbound'
    addressPrefixes: [clientAddressPrefix]
  }
}

resource association 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2024-07-01' = {
  parent: perimeter
  name: 'storage'
  properties: {
    accessMode: 'Enforced'
    privateLinkResource: { id: storageResourceId }
    profile: { id: profile.id }
  }
  dependsOn: [subscriptionRule, clientRule]
}

output resourceId string = perimeter.id

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(workspaceResourceId)) {
  name: 'perimeter-access'
  scope: perimeter
  properties: {
    workspaceId: workspaceResourceId
    logs: [
      { category: 'NspPublicInboundPerimeterRulesAllowed', enabled: true }
      { category: 'NspPublicInboundPerimeterRulesDenied', enabled: true }
      { category: 'NspPublicInboundResourceRulesDenied', enabled: true }
    ]
  }
}
