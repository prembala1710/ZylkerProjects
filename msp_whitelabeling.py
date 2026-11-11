"""
ZylkerProjects - MSP White-Labeling & Custom Branding

This module enables Managed Service Providers (MSPs) to offer custom-branded
portals to their tenants. It supports custom domain mapping, logo uploads,
and UI color theme personalization to strengthen the MSP's brand presence.
"""

class WhiteLabelConfig:
    def __init__(self, tenant_id, primary_color="#000000", secondary_color="#FFFFFF", logo_url="default.png", custom_domain=None):
        self.tenant_id = tenant_id
        self.primary_color = primary_color
        self.secondary_color = secondary_color
        self.logo_url = logo_url
        self.custom_domain = custom_domain
        self.is_active = True

    def update_branding(self, primary=None, secondary=None, logo=None):
        if primary:
            self.primary_color = primary
        if secondary:
            self.secondary_color = secondary
        if logo:
            self.logo_url = logo
            
    def set_custom_domain(self, domain):
        self.custom_domain = domain

    def get_css_payload(self):
        return {
            "--primary-color": self.primary_color,
            "--secondary-color": self.secondary_color,
            "logo": self.logo_url
        }

class MSPBrandingManager:
    def __init__(self):
        self.tenant_configs = {}

    def enable_whitelabeling(self, tenant_id, **kwargs):
        config = WhiteLabelConfig(tenant_id, **kwargs)
        self.tenant_configs[tenant_id] = config
        return config

    def get_tenant_branding(self, tenant_id):
        if tenant_id in self.tenant_configs:
            return self.tenant_configs[tenant_id].get_css_payload()
        return None

if __name__ == "__main__":
    branding_manager = MSPBrandingManager()
    
    # Configure branding for Acme Corp
    acme_config = branding_manager.enable_whitelabeling(
        tenant_id="T-01",
        primary_color="#FF5733",
        secondary_color="#C70039",
        logo_url="https://acmecorp.com/logo.png",
        custom_domain="projects.acmecorp.com"
    )
    
    print("Acme Corp Branding Details:")
    print(branding_manager.get_tenant_branding("T-01"))
