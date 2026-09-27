"""Microsoft Entra ID sign-in. Roles come from Entra group membership."""
from django.conf import settings
from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class EntraBackend(OIDCAuthenticationBackend):
    def get_userinfo(self, access_token, id_token, payload):
        # Graph's userinfo has no "groups"; Entra puts them in the verified ID token (enable the groups claim in the app registration).
        return {**payload, **super().get_userinfo(access_token, id_token, payload)}

    def _roles(self, claims):
        m = getattr(settings, "OIDC_GROUP_ROLE_MAP", {})
        return sorted({m[g] for g in claims.get("groups", []) if g in m})

    def create_user(self, claims):
        user = super().create_user(claims)
        return self.update_user(user, claims)

    def update_user(self, user, claims):
        user.display_name = claims.get("name", "") or user.display_name
        user.email = claims.get("email", user.email)
        user.roles = self._roles(claims)
        user.save()
        return user
