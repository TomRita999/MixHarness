#import <AppKit/AppKit.h>

// Read the same AppKit attributes that window attachments and screen readers request.
static BOOL containsText(id element, NSString *text, NSHashTable *visited)
{
    if ([visited containsObject:element])
        return NO;
    [visited addObject:element];
    if ([element respondsToSelector:@selector(accessibilitySelectedChildren)]) {
        for (id selected in [element accessibilitySelectedChildren]) {
            if ([selected respondsToSelector:@selector(accessibilityRole)])
                (void)[selected accessibilityRole];
        }
    }
    BOOL found = NO;
    if ([element respondsToSelector:@selector(accessibilityLabel)])
        found = [[element accessibilityLabel] containsString:text];
    if ([element respondsToSelector:@selector(accessibilityValue)]) {
        id value = [element accessibilityValue];
        if ([value isKindOfClass:NSString.class] && [value containsString:text])
            found = YES;
    }
    if ([element respondsToSelector:@selector(accessibilityChildren)]) {
        for (id child in [element accessibilityChildren]) {
            if (containsText(child, text, visited))
                found = YES;
        }
    }
    return found;
}

int windowContainsText(void *nativeView, const char *text)
{
    @autoreleasepool {
        NSView *view = (__bridge NSView *)nativeView;
        NSHashTable *visited = [NSHashTable hashTableWithOptions:NSPointerFunctionsObjectPointerPersonality];
        return containsText(view.window, [NSString stringWithUTF8String:text], visited);
    }
}
