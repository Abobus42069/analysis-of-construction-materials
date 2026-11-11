"""Retail offers are separate observations; compare confirmed identities only."""
import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from datetime import datetime, timezone
from .core import identity


def number(value, field, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid numeric field: '+field) from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('Invalid numeric field: '+field)
    return result


def coordinates(value):
    if not isinstance(value, dict):
        raise ValueError('Coordinates require lat/lon')
    lat, lon = float(value['lat']), float(value['lon'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Coordinates out of range')
    return lat, lon


def distance_km(a, b):
    lat1, lon1 = map(math.radians, coordinates(a))
    lat2, lon2 = map(math.radians, coordinates(b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def validate_offer(offer):
    required = ['retailer', 'retailer_sku', 'source_url', 'observed_at', 'city',
                'price', 'currency', 'pack_quantity', 'quantity_unit', 'vat_included',
                'matching_status', 'availability']
    for key in required:
        if key not in offer or offer[key] is None:
            raise ValueError('Missing offer field: '+key)
    number(offer['price'], 'price', True)
    number(offer['pack_quantity'], 'pack_quantity', True)
    if offer.get('delivery_price') is not None:
        number(offer['delivery_price'], 'delivery_price')
    if offer.get('coordinates') is not None:
        coordinates(offer['coordinates'])
    timestamp = datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('observed_at requires a timezone')
    if timestamp > datetime.now(timezone.utc):
        raise ValueError('observed_at cannot be in the future')
    if offer['vat_included'] not in [True, False] or not isinstance(offer['vat_included'], bool):
        raise ValueError('vat_included must be a boolean')
    if offer['matching_status'] not in ['confirmed', 'needs_review', 'unmatched']:
        raise ValueError('Invalid matching status')
    if offer['matching_status'] == 'confirmed' and (not offer.get('material_id') or not offer.get('manufacturer_variant_id')):
        raise ValueError('Confirmed offer requires material_id and manufacturer_variant_id')
    if offer['availability'] not in ['in_stock', 'out_of_stock', 'unknown', 'on_order']:
        raise ValueError('Invalid availability')
    if offer.get('minimum_packs') is not None:
        minimum = number(offer['minimum_packs'], 'minimum_packs', True)
        if minimum != minimum.to_integral_value():
            raise ValueError('minimum_packs must be an integer')
    result = dict(offer)
    result['price'] = str(number(offer['price'], 'price', True))
    result['pack_quantity'] = str(number(offer['pack_quantity'], 'pack_quantity', True))
    result['id'] = identity(offer['retailer'], offer['retailer_sku'], offer['city'],
                            offer.get('store_id'), offer['observed_at'], result['price'])
    return result


def rank_offers(offers, material_id, city, quantity, unit, currency='RUB',
                origin=None, radius_km=None, delivered=False, max_age_hours=24, as_of=None,
                variant_id=None, delivery_destination=None, include_nearby=False, district=None):
    needed = number(quantity, 'quantity', True)
    if not variant_id:
        raise ValueError('Specify the exact manufacturer variant_id')
    if delivered and not delivery_destination:
        raise ValueError('Delivered comparison requires a specific delivery destination')
    if max_age_hours <= 0 or not math.isfinite(max_age_hours):
        raise ValueError('max_age_hours must be positive')
    if radius_km is not None and (origin is None or not math.isfinite(radius_km) or radius_km <= 0):
        raise ValueError('Positive radius requires an origin')
    if origin is not None:
        coordinates(origin)
    if include_nearby and (origin is None or radius_km is None):
        raise ValueError('Nearby cities require origin and radius_km')
    current = as_of or datetime.now(timezone.utc)
    ranked, excluded = [], []
    for raw in offers:
        try:
            offer = validate_offer(raw)
            if offer['matching_status'] != 'confirmed' or offer.get('material_id') != material_id or offer.get('manufacturer_variant_id') != variant_id:
                raise ValueError('identity_not_confirmed')
            if not include_nearby and offer['city'].casefold() != city.casefold():
                raise ValueError('different_city')
            if district is not None and (offer.get('district') or '').casefold() != district.casefold():
                raise ValueError('different_or_unknown_district')
            if offer['quantity_unit'] != unit or offer['currency'] != currency or not offer['vat_included']:
                raise ValueError('incomparable_unit_currency_or_tax')
            age = (current-datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))).total_seconds()/3600
            if age < 0 or age > max_age_hours:
                raise ValueError('observation_not_current')
            if offer['availability'] != 'in_stock':
                raise ValueError('availability_not_confirmed')
            distance = None
            if origin is not None and offer.get('coordinates'):
                distance = distance_km(origin, offer['coordinates'])
            if radius_km is not None and (distance is None or distance > radius_km):
                raise ValueError('outside_radius_or_location_unknown')
            packs = max(int((needed/number(offer['pack_quantity'], 'pack_quantity', True)).to_integral_value(rounding=ROUND_CEILING)),
                        int(offer.get('minimum_packs', 1)))
            if offer.get('available_packs') is not None and number(offer['available_packs'], 'available_packs') < packs:
                raise ValueError('insufficient_stock')
            if delivered and (offer.get('delivery_price') is None or offer.get('delivery_destination') != delivery_destination):
                raise ValueError('delivery_price_or_destination_unknown')
            goods = number(offer['price'], 'price', True)*packs
            total = goods + (number(offer['delivery_price'], 'delivery_price') if delivered else Decimal(0))
            ranked.append({**offer, 'required_packs': packs, 'goods_total': str(goods),
                'comparison_total': str(total), 'comparison_mode': 'delivered' if delivered else 'goods_only',
                'price_per_unit': str(number(offer['price'], 'price')/number(offer['pack_quantity'], 'pack_quantity', True)),
                'distance_km': distance})
        except (ValueError, KeyError, TypeError) as exc:
            excluded.append({'offer': raw.get('id', raw.get('source_url')), 'reason': str(exc)})
    ranked.sort(key=lambda x: (Decimal(x['comparison_total']), x['id']))
    return {'ranked': ranked, 'excluded': excluded,
            'distance_method': 'straight_line_haversine_not_delivery_or_road_distance'}
