"""
ZylkerProjects - MSP Multi-Tenant Dashboard MVP
This module provides a high-value Managed Service Provider (MSP) dashboard
allowing MSPs to manage multiple client tenants, monitor service health,
and automate billing from a single pane of glass.
"""

class Tenant:
    def __init__(self, tenant_id, name, subscription_tier):
        self.tenant_id = tenant_id
        self.name = name
        self.subscription_tier = subscription_tier
        self.services = []
        self.health_status = "Healthy"

    def add_service(self, service_name):
        self.services.append(service_name)

class MSPDashboard:
    def __init__(self):
        self.tenants = {}

    def register_tenant(self, tenant_id, name, tier):
        new_tenant = Tenant(tenant_id, name, tier)
        self.tenants[tenant_id] = new_tenant
        return new_tenant

    def generate_billing_report(self):
        report = []
        for t_id, tenant in self.tenants.items():
            base_cost = 100 if tenant.subscription_tier == 'Basic' else 500
            service_cost = len(tenant.services) * 50
            total = base_cost + service_cost
            report.append({'tenant': tenant.name, 'total_billed': total})
        return report

    def get_health_overview(self):
        return {
            t.name: t.health_status for t in self.tenants.values()
        }

if __name__ == "__main__":
    dashboard = MSPDashboard()
    t1 = dashboard.register_tenant("T-01", "Acme Corp", "Premium")
    t1.add_service("Cloud Backup")
    t1.add_service("Endpoint Security")

    t2 = dashboard.register_tenant("T-02", "Globex", "Basic")
    t2.add_service("Email Hosting")

    print("MSP Billing Report:", dashboard.generate_billing_report())
    print("MSP Health Overview:", dashboard.get_health_overview())
