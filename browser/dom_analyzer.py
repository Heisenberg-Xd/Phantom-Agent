"""
Phantom QA Agent v3.0 - DOM Analyzer & Universal Utilities
Implements Phase 0: Intelligent Website Analysis and Step 3: Robust Selectors
"""
import re
from urllib.parse import urljoin, urlparse

def get_tech_stack_detection_script():
    return """
    () => {
        return {
            react: !!document.querySelector('[data-reactroot], [data-reactid]'),
            vue: !!window.Vue || !!document.querySelector('[data-v-]'),
            angular: !!window.angular || !!document.querySelector('[ng-app], [ng-controller]'),
            jquery: !!window.jQuery,
            wordpress: !!document.querySelector('meta[content*="WordPress"]'),
            shopify: !!window.Shopify,
            wix: !!document.querySelector('[data-wix-]'),
            nextjs: !!document.querySelector('#__next'),
            gatsby: !!document.querySelector('#___gatsby')
        };
    }
    """

async def discover_all_interactive_elements(page):
    elements = {
        'buttons': [],
        'links': [],
        'inputs': [],
        'dropdowns': [],
        'modals': [],
        'file_uploads': []
    }
    
    # Buttons
    buttons = await page.query_selector_all('button, [role="button"], [type="button"], [type="submit"], a.btn, .button')
    for btn in buttons:
        text = await btn.inner_text() or await btn.get_attribute('aria-label') or await btn.get_attribute('title') or ""
        elements['buttons'].append({
            'text': text.strip(),
            'selector': await generate_robust_selector(page, btn),
            'visible': await btn.is_visible(),
            'enabled': await btn.is_enabled()
        })
    
    # Links
    links = await page.query_selector_all('a[href]')
    for link in links:
        href = await link.get_attribute('href')
        if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            text = await link.inner_text()
            elements['links'].append({
                'text': text.strip(),
                'href': href,
                'selector': await generate_robust_selector(page, link),
                'is_external': not href.startswith('/') and urlparse(href).netloc != urlparse(page.url).netloc
            })
    
    # Inputs
    inputs = await page.query_selector_all('input:not([type="hidden"]), textarea')
    for inp in inputs:
        elements['inputs'].append({
            'type': await inp.get_attribute('type') or 'text',
            'name': await inp.get_attribute('name'),
            'selector': await generate_robust_selector(page, inp)
        })
    
    # Dropdowns
    selects = await page.query_selector_all('select')
    for select in selects:
        options_elements = await select.query_selector_all('option')
        options = []
        for opt in options_elements:
            options.append(await opt.inner_text())
        elements['dropdowns'].append({
            'name': await select.get_attribute('name'),
            'selector': await generate_robust_selector(page, select),
            'options': options
        })
    
    return elements

async def discover_all_forms(page):
    forms = []
    form_elements = await page.query_selector_all('form')
    
    for form in form_elements:
        form_data = {
            'action': await form.get_attribute('action'),
            'method': await form.get_attribute('method') or 'get',
            'inputs': [],
            'buttons': [],
            'purpose': None
        }
        
        inputs = await form.query_selector_all('input, textarea, select')
        for inp in inputs:
            type_attr = await inp.get_attribute('type')
            label_text = ""
            # Try to find associated label
            id_attr = await inp.get_attribute('id')
            if id_attr:
                label_elem = await page.query_selector(f'label[for="{id_attr}"]')
                if label_elem:
                    label_text = await label_elem.inner_text()
                    
            input_data = {
                'type': type_attr or 'text',
                'name': await inp.get_attribute('name') or '',
                'id': id_attr or '',
                'placeholder': await inp.get_attribute('placeholder') or '',
                'required': await inp.get_attribute('required') is not None,
                'label': label_text,
                'aria_label': await inp.get_attribute('aria-label') or '',
                'selector': await generate_robust_selector(page, inp)
            }
            form_data['inputs'].append(input_data)
        
        buttons = await form.query_selector_all('button[type="submit"], input[type="submit"]')
        for btn in buttons:
            btn_text = await btn.inner_text() or await btn.get_attribute('value') or ""
            form_data['buttons'].append({
                'text': btn_text.strip(), 
                'selector': await generate_robust_selector(page, btn)
            })
            
        form_data['purpose'] = infer_form_purpose(form_data)
        forms.append(form_data)
        
    return forms

def infer_form_purpose(form_data):
    all_text = ' '.join(
        [inp.get('label', '') + ' ' + inp.get('placeholder', '') + ' ' + inp.get('name', '') 
         for inp in form_data['inputs']]
    ).lower()
    
    if any(word in all_text for word in ['email', 'password', 'username', 'login', 'signin', 'uid', 'passw']):
        return 'login'
    elif any(word in all_text for word in ['register', 'signup', 'create account']):
        return 'registration'
    elif any(word in all_text for word in ['search', 'query', 'find', 'q']):
        return 'search'
    elif any(word in all_text for word in ['contact', 'message', 'inquiry', 'feedback']):
        return 'contact'
    elif any(word in all_text for word in ['payment', 'card', 'checkout', 'billing']):
        return 'payment'
    elif any(word in all_text for word in ['subscribe', 'newsletter', 'updates']):
        return 'subscription'
    return 'generic'

async def generate_robust_selector(page, element):
    selectors = []
    
    test_id = await element.get_attribute('data-testid') or await element.get_attribute('data-test')
    if test_id: selectors.append(f'[data-testid="{test_id}"]')
    
    elem_id = await element.get_attribute('id')
    if elem_id and not any(c.isdigit() for c in elem_id): # ignore auto-generated jsf ids
        selectors.append(f'#{elem_id}')
        
    name = await element.get_attribute('name')
    if name: selectors.append(f'[name="{name}"]')
    
    aria_label = await element.get_attribute('aria-label')
    if aria_label: selectors.append(f'[aria-label="{aria_label}"]')
    
    placeholder = await element.get_attribute('placeholder')
    if placeholder: selectors.append(f'[placeholder="{placeholder}"]')
    
    # class fallback
    classes = await element.get_attribute('class')
    if classes:
        class_list = [c for c in classes.split() if len(c) > 2]
        if 0 < len(class_list) <= 3:
            selectors.append('.' + '.'.join(class_list))
            
    # evaluate xpath as last resort
    xpath = await page.evaluate('''
        (el) => {
            if (el.id) return `//*[@id="${el.id}"]`;
            let path = '';
            while (el.nodeType === Node.ELEMENT_NODE) {
                let sibling = el.previousSibling;
                let count = 1;
                while (sibling) {
                    if (sibling.nodeType === Node.ELEMENT_NODE && sibling.nodeName === el.nodeName) count++;
                    sibling = sibling.previousSibling;
                }
                path = '/' + el.nodeName.toLowerCase() + '[' + count + ']' + path;
                el = el.parentNode;
            }
            return path;
        }
    ''', element)
    if xpath: selectors.append(xpath)
    
    return {
        'primary': selectors[0] if selectors else None,
        'fallbacks': selectors[1:] if len(selectors) > 1 else [],
        'all': selectors
    }

async def smart_find_element(page, selector_obj, timeout=5000):
    if not isinstance(selector_obj, dict) or 'all' not in selector_obj:
        try:
            return page.locator(selector_obj).first
        except:
            return None
            
    for selector in selector_obj['all']:
        try:
            element = page.locator(selector).first
            await element.wait_for(state='visible', timeout=timeout)
            return element
        except:
            continue
    return None

async def extract_internal_links(page, base_url):
    base_domain = urlparse(base_url).netloc
    links = []
    
    all_links = await page.query_selector_all('a[href]')
    for link in all_links:
        href = await link.get_attribute('href')
        if not href: continue
        absolute_url = urljoin(page.url, href)
        parsed = urlparse(absolute_url)
        
        if (parsed.netloc == base_domain and 
            not parsed.fragment and 
            not parsed.scheme in ['mailto', 'tel', 'javascript']):
            links.append(absolute_url)
            
    return list(set(links))
