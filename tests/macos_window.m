#import <AppKit/AppKit.h>

// Read the same AppKit attributes that window attachments and screen readers request.
static BOOL containsText(id element, NSString *text, NSHashTable *visited)
{
    if ([visited containsObject:element])
        return NO;
    [visited addObject:element];
    NSString *title = [element respondsToSelector:@selector(accessibilityTitle)] ? [element accessibilityTitle] : nil;
    NSString *label = [element respondsToSelector:@selector(accessibilityLabel)] ? [element accessibilityLabel] : nil;
    id value = [element respondsToSelector:@selector(accessibilityValue)] ? [element accessibilityValue] : nil;
    BOOL found = [title containsString:text] || [label containsString:text] ||
                 ([value isKindOfClass:NSString.class] && [value containsString:text]);
    if ([element respondsToSelector:@selector(accessibilityChildren)]) {
        for (id child in [element accessibilityChildren]) {
            if (containsText(child, text, visited))
                found = YES;
        }
    }
    if ([element respondsToSelector:@selector(accessibilitySelectedChildren)]) {
        for (id selected in [element accessibilitySelectedChildren]) {
            if ([selected respondsToSelector:@selector(accessibilityRole)])
                (void)[selected accessibilityRole];
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
