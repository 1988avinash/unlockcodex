"""Immutable host-selected routes. No discovery, credential reads or activation."""
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit
import re


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    provider_id: str
    actual_model: str
    native_alias: str
    endpoint: str
    protocol_profile: str
    supports_native_tools: bool
    auto_fallback_qualified: bool
    qualification_ref: str
    display_name: str = ''


class RouteRegistry:
    def __init__(self, routes, *, auto_fallback_id):
        routes=tuple(routes)
        if not routes or len(routes)>40:raise ValueError('Bounded explicit routes required')
        ids={};aliases={}
        endpoints={}
        for spec in routes:
            if not isinstance(spec,RouteSpec):raise ValueError('Exact immutable RouteSpec required')
            if (not re.fullmatch('[a-z0-9][a-z0-9:_-]{0,127}',spec.route_id) or spec.route_id in ('openai','pause')
                    or spec.route_id in ids):raise ValueError('Unique plan route identity required')
            if (not re.fullmatch('tp-[a-z0-9-]{1,120}',spec.native_alias) or spec.native_alias in aliases):
                raise ValueError('Unique qualified plan alias required')
            # Providers are user configuration: any HTTPS Responses endpoint, exactly one
            # per provider id so a provider's key is never sent to a second host.
            target=urlsplit(spec.endpoint) if isinstance(spec.endpoint,str) else None
            if (not isinstance(spec.provider_id,str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}',spec.provider_id)
                    or spec.provider_id=='openai' or target is None
                    or endpoints.setdefault(spec.provider_id,spec.endpoint)!=spec.endpoint
                    or not (spec.endpoint=='agy:session-only-structured' if spec.provider_id=='antigravity' else
                        target.scheme=='https' and target.hostname and not (target.username or target.password or target.query or target.fragment))):
                raise ValueError('One HTTPS Responses endpoint per provider id is required')
            if not isinstance(spec.actual_model,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',spec.actual_model):
                raise ValueError('Exact model identifier required')
            if spec.protocol_profile not in ('responses-full','responses-lite','agy-structured'):
                raise ValueError('Qualified protocol profile required')
            if (spec.provider_id=='antigravity')!=(spec.protocol_profile=='agy-structured'):
                raise ValueError('Signed-in agent and Responses profiles cannot be interchanged')
            if type(spec.supports_native_tools) is not bool or type(spec.auto_fallback_qualified) is not bool:
                raise ValueError('Explicit boolean qualification required')
            if not spec.supports_native_tools:raise ValueError('Text-only routes cannot be enrolled for native coding')
            if not isinstance(spec.qualification_ref,str) or not 0<len(spec.qualification_ref)<=1000:
                raise ValueError('Host-reviewed qualification reference required')
            if not isinstance(spec.display_name,str) or len(spec.display_name)>200:raise ValueError('Bounded display label required')
            ids[spec.route_id]=spec;aliases[spec.native_alias]=spec
        if auto_fallback_id not in ids or not ids[auto_fallback_id].auto_fallback_qualified:
            raise ValueError('Automatic fallback must be explicitly qualified')
        self.routes=routes;self.auto_fallback_id=auto_fallback_id
        self._ids=MappingProxyType(ids);self._aliases=MappingProxyType(aliases)

    def resolve_alias(self,model):return self._aliases.get(model)
    def get(self,route_id):return self._ids[route_id]

